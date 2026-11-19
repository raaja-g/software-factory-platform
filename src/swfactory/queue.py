"""Job queue: enqueue with a safe per-app sequence, per-app-limited claims, leases."""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.db.tables import App, Job
from swfactory.definition.models import AppDef
from swfactory.definition.store import NoActiveDefinition, load_active
from swfactory.domain.types import ACTIVE_STAGES, JobKind, Stage
from swfactory.events import record_event

_ACTIVE = sorted(st.value for st in ACTIVE_STAGES)
_NORMAL_KINDS = "('normal', 'requirements')"
_CLAIMABLE = (
    "(stage = 'queued' OR stage = ANY(CAST(:active_stages AS text[]))) "
    "AND (lease_until IS NULL OR lease_until < now()) "
    "AND (retry_after IS NULL OR retry_after <= now()) "
    "AND id <> ALL(CAST(CAST(:exclude AS text[]) AS uuid[]))"
)


class UnknownApp(LookupError):
    pass


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len].strip("-")
    return slug or "job"


def branch_name(seq: int, slug: str) -> str:
    return f"factory/{seq:03d}-{slug}"


def ci_requires_approval(app: AppDef, kind: JobKind) -> bool:
    """A normal job of a github app that is not ci_safe waits for a person before any push."""
    return kind == JobKind.NORMAL and app.repo_kind == "github" and not app.github.ci_safe


async def enqueue_job(
    s: AsyncSession,
    *,
    app_slug: str,
    feature: str,
    title: str | None,
    source: str,
    source_ref: str | None,
    requested_by: str,
    kind: JobKind = JobKind.NORMAL,
    profile_name: str | None = None,
    budget_usd: Decimal | None = None,
    branch: str | None = None,
    base_sha: str | None = None,
    definition_sha: str | None = None,
    backlog_id: UUID | None = None,
    backlog_pos: int | None = None,
    require_human_approval: bool = False,
    follow_up_of: UUID | None = None,
) -> Job:
    """Insert a queued job in the caller's transaction; never commits."""
    if backlog_pos is not None and backlog_id is None:
        raise ValueError("backlog_pos needs backlog_id")
    if (branch is not None or base_sha is not None) and follow_up_of is None:
        raise ValueError("branch and base_sha are only for follow-up jobs")
    found = await load_active(s)
    if found is None:
        raise NoActiveDefinition("no active definition")
    active_sha, defn = found
    app_def = defn.factory.apps.get(app_slug)
    app_id = await s.scalar(
        text("SELECT id FROM app.apps WHERE slug = :slug AND archived_at IS NULL FOR UPDATE"),
        {"slug": app_slug},
    )
    if app_id is None or app_def is None:
        raise UnknownApp(app_slug)
    if profile_name and app_def.allowed_profiles and profile_name not in app_def.allowed_profiles:
        raise ValueError(f"profile '{profile_name}' is not allowed for app '{app_slug}'")
    seq = int(
        await s.scalar(select(func.coalesce(func.max(Job.seq), 0) + 1).where(Job.app_id == app_id))
        or 1
    )
    first_line = next((ln for ln in feature.splitlines() if ln.strip()), "").strip()
    job_title = (title or first_line)[:80]
    slug = slugify(job_title)
    budget = budget_usd or app_def.budget_usd or defn.factory.budgets.job_usd
    max_rounds = app_def.max_rounds or defn.factory.defaults.max_rounds
    job = Job(
        app_id=app_id,
        seq=seq,
        slug=slug,
        title=job_title,
        feature=feature,
        kind=kind.value,
        source=source,
        source_ref=source_ref,
        requested_by=requested_by,
        definition_sha=definition_sha or active_sha,
        profile_name=profile_name,
        branch=branch or branch_name(seq, slug),
        base_sha=base_sha,
        stage=Stage.QUEUED.value,
        round=0,
        max_rounds=max_rounds,
        budget_usd=budget,
        backlog_id=backlog_id,
        backlog_pos=backlog_pos,
        require_human_approval=require_human_approval or ci_requires_approval(app_def, kind),
        follow_up_of=follow_up_of,
    )
    s.add(job)
    await s.flush()
    await record_event(
        s,
        job.id,
        "job.created",
        {"app": app_slug, "seq": seq, "title": job_title, "source": source},
    )
    return job


async def _candidates(s: AsyncSession, exclude: list[str]) -> list[tuple[str, str | None]]:
    """(job id, app id or None for a benchmark) oldest first."""
    found = await load_active(s)
    if found is None:
        return []
    defn = found[1]
    apps = (await s.execute(select(App.id, App.slug).where(App.archived_at.is_(None)))).all()
    limits = {
        str(i): (defn.factory.apps[slug].concurrency or defn.factory.concurrency.per_app)
        for i, slug in apps
        if slug in defn.factory.apps
    }
    out: list[tuple[datetime, str, str | None]] = []
    params = {"limits": json.dumps(limits), "active_stages": _ACTIVE, "exclude": exclude}
    rows = (
        await s.execute(
            text(
                f"""
                SELECT DISTINCT ON (j.app_id) j.id, j.app_id, j.created_at
                  FROM app.jobs j JOIN jsonb_each_text(CAST(:limits AS jsonb)) l(app_id, lim)
                    ON j.app_id = l.app_id::uuid
                 WHERE j.kind IN {_NORMAL_KINDS} AND app.backlog_turn(j)
                   AND (j.stage = 'queued' OR j.stage = ANY(CAST(:active_stages AS text[])))
                   AND (j.lease_until IS NULL OR j.lease_until < now())
                   AND (j.retry_after IS NULL OR j.retry_after <= now())
                   AND j.id <> ALL(CAST(CAST(:exclude AS text[]) AS uuid[]))
                   AND (SELECT count(*) FROM app.jobs r WHERE r.app_id = j.app_id
                          AND r.kind IN {_NORMAL_KINDS}
                          AND r.lease_owner IS NOT NULL AND r.lease_until > now()) < l.lim::int
                 ORDER BY j.app_id, j.created_at, j.id
                """
            ),
            params,
        )
    ).all()
    for jid, aid, created in rows:
        out.append((created, str(jid), str(aid)))
    bench = (
        await s.execute(
            text(
                f"SELECT id, created_at FROM app.jobs WHERE kind = 'benchmark' AND {_CLAIMABLE} "
                "ORDER BY created_at, id LIMIT 1"
            ),
            {"active_stages": _ACTIVE, "exclude": exclude},
        )
    ).first()
    if bench is not None:
        out.append((bench[1], str(bench[0]), None))
    out.sort(key=lambda t: t[0])
    return [(jid, aid) for _, jid, aid in out]


_UPDATE = """
UPDATE app.jobs SET lease_owner = :worker_id,
       lease_until = now() + make_interval(secs => :lease_s), lease_epoch = lease_epoch + 1,
       crash_count = crash_count + CASE WHEN lease_owner IS NOT NULL THEN 1 ELSE 0 END,
       retry_after = CASE WHEN lease_owner IS NOT NULL
                          THEN now() + make_interval(secs => 60 * power(2, crash_count))
                          ELSE retry_after END,
       started_at = coalesce(started_at, now()), updated_at = now() WHERE id = :id RETURNING *
"""


async def _try_claim(
    s: AsyncSession,
    job_id: str,
    app_id: str | None,
    worker_id: str,
    lease_s: int,
    exclude: list[str],
) -> Job | None:
    async with s.begin():
        params = {"active_stages": _ACTIVE, "exclude": exclude}
        if app_id is None:
            row = await s.scalar(
                text(
                    f"SELECT id FROM app.jobs WHERE id = CAST(:id AS uuid) AND kind = 'benchmark' "
                    f"AND {_CLAIMABLE} FOR UPDATE SKIP LOCKED"
                ),
                {**params, "id": job_id},
            )
        else:
            await s.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('swfactory.app:' || :app_id))"),
                {"app_id": app_id},
            )
            defn = await load_active(s)
            if defn is None:
                return None
            slug = await s.scalar(
                text(
                    "SELECT slug FROM app.apps WHERE id = CAST(:a AS uuid) AND archived_at IS NULL"
                ),
                {"a": app_id},
            )
            app_def = defn[1].factory.apps.get(slug) if slug else None
            if app_def is None:
                return None
            limit = app_def.concurrency or defn[1].factory.concurrency.per_app
            held = await s.scalar(
                text(
                    f"SELECT count(*) FROM app.jobs WHERE app_id = CAST(:a AS uuid) "
                    f"AND kind IN {_NORMAL_KINDS} AND ((lease_owner IS NOT NULL "
                    "AND lease_until > now())"
                    + (
                        " OR (kind = 'normal' AND stage = 'delivered')"
                        if app_def.hold_slot_while_pr_open
                        else ""
                    )
                    + ")"
                ),
                {"a": app_id},
            )
            if (held or 0) >= limit:
                return None
            row = await s.scalar(
                text(
                    f"SELECT id FROM app.jobs j WHERE app_id = CAST(:a AS uuid) "
                    f"AND kind IN {_NORMAL_KINDS} AND app.backlog_turn(j) AND {_CLAIMABLE} "
                    "ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"
                ),
                {**params, "a": app_id},
            )
        if row is None:
            return None
        return (
            await s.execute(
                select(Job).from_statement(text(_UPDATE)),
                {"worker_id": worker_id, "lease_s": lease_s, "id": row},
            )
        ).scalar_one()


async def claim_next_job(
    s: AsyncSession, *, worker_id: str, lease_s: int, exclude: Collection[UUID] = ()
) -> Job | None:
    """Claim the oldest claimable job of an app under its limit; needs no open transaction.

    Every claim bumps ``lease_epoch``. Jobs in ``exclude`` (the ones the caller is already
    driving) are never returned.
    """
    skip = sorted(str(j) for j in exclude)
    async with s.begin():
        cands = await _candidates(s, skip)
    for job_id, app_id in cands:
        job = await _try_claim(s, job_id, app_id, worker_id, lease_s, skip)
        if job is not None:
            return job
    return None


async def renew_lease(
    s: AsyncSession, *, job_id: UUID, worker_id: str, lease_epoch: int, lease_s: int
) -> bool:
    async with s.begin():
        res = await s.execute(
            text(
                "UPDATE app.jobs SET lease_until = now() + make_interval(secs => :lease_s) "
                "WHERE id = :id AND lease_owner = :w AND lease_epoch = :e RETURNING id"
            ),
            {"lease_s": lease_s, "id": job_id, "w": worker_id, "e": lease_epoch},
        )
        return res.first() is not None


async def release_job(s: AsyncSession, *, job_id: UUID, worker_id: str, lease_epoch: int) -> None:
    async with s.begin():
        await s.execute(
            text(
                "UPDATE app.jobs SET lease_owner = NULL, lease_until = NULL "
                "WHERE id = :id AND lease_owner = :w AND lease_epoch = :e"
            ),
            {"id": job_id, "w": worker_id, "e": lease_epoch},
        )
