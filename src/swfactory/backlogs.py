"""Backlogs: a requirement turned into ordered jobs, approved once, run strictly in order."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.db.tables import Backlog, Job
from swfactory.domain.types import TERMINAL_STAGES, BacklogStatus, JobKind, Stage
from swfactory.events import record_event
from swfactory.queue import UnknownApp, enqueue_job

_REF_RE = re.compile(r"^([^:\s]+):b(\d+)$")
_BLOCKING = {Stage.CLOSED.value, Stage.FAILED.value, Stage.NEEDS_HUMAN.value}


@dataclass(frozen=True, slots=True)
class RequirementItem:
    id: str
    title: str
    feature: str
    depends_on: tuple[str, ...]
    acceptance: tuple[str, ...]


class BacklogError(ValueError):
    """A refused backlog action; the api maps it to 409."""


def format_backlog_ref(app_slug: str, seq: int) -> str:
    return f"{app_slug}:b{seq:03d}"


def parse_backlog_ref(ref: str) -> UUID | tuple[str, int]:
    try:
        return UUID(ref)
    except ValueError:
        pass
    m = _REF_RE.match(ref)
    if m is None:
        raise ValueError(f"invalid backlog reference '{ref}'")
    return m.group(1), int(m.group(2))


def render_item_feature(item: RequirementItem) -> str:
    out = f"{item.title}\n\n{item.feature}"
    if item.acceptance:
        out += "\n\n## Acceptance criteria\n" + "\n".join(f"- [ ] {a}" for a in item.acceptance)
    return out


async def _slug(s: AsyncSession, bl: Backlog) -> str:
    return str(await s.scalar(text("SELECT slug FROM app.apps WHERE id = :i"), {"i": bl.app_id}))


async def _locked(s: AsyncSession, backlog_id: UUID) -> Backlog:
    bl = await s.scalar(
        select(Backlog)
        .where(Backlog.id == backlog_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if bl is None:
        raise BacklogError(f"backlog {backlog_id} not found")
    return bl


async def _feature_jobs(s: AsyncSession, backlog_id: UUID) -> list[Job]:
    rows = await s.scalars(
        select(Job)
        .where(Job.backlog_id == backlog_id, Job.backlog_pos.is_not(None))
        .order_by(Job.backlog_pos)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def _emit(
    s: AsyncSession, bl: Backlog, kind: str, extra: dict[str, Any] | None = None
) -> None:
    slug = await _slug(s, bl)
    job_id = bl.requirements_job_id
    if job_id is None:
        job_id = await s.scalar(
            select(Job.id)
            .where(Job.backlog_id == bl.id, Job.backlog_pos.is_not(None))
            .order_by(Job.backlog_pos)
            .limit(1)
        )
    await record_event(
        s,
        job_id,
        kind,
        {
            "backlog_id": bl.id,
            "ref": format_backlog_ref(slug, bl.seq),
            "app": slug,
            **(extra or {}),
        },
    )


async def _new_backlog(
    s: AsyncSession, app_slug: str, title: str, created_by: str, status: BacklogStatus
) -> Backlog:
    app_id = await s.scalar(
        text("SELECT id FROM app.apps WHERE slug = :slug AND archived_at IS NULL FOR UPDATE"),
        {"slug": app_slug},
    )
    if app_id is None:
        raise UnknownApp(app_slug)
    seq = int(
        await s.scalar(
            select(func.coalesce(func.max(Backlog.seq), 0) + 1).where(Backlog.app_id == app_id)
        )
        or 1
    )
    bl = Backlog(app_id=app_id, seq=seq, title=title, status=status.value, created_by=created_by)
    s.add(bl)
    await s.flush()
    return bl


def _title(title: str | None, requirement: str) -> str:
    if title:
        return title[:80]
    first = next((ln for ln in requirement.splitlines() if ln.strip()), "").strip()
    return first[:80]


async def create_backlog(
    s: AsyncSession,
    *,
    app_slug: str,
    requirement: str,
    requested_by: str,
    title: str | None = None,
    profile_name: str | None = None,
    budget_usd: Decimal | None = None,
    source: str = "api",
) -> tuple[Backlog, Job]:
    bl = await _new_backlog(
        s, app_slug, _title(title, requirement), requested_by, BacklogStatus.DRAFTING
    )
    job = await enqueue_job(
        s,
        app_slug=app_slug,
        feature=requirement,
        title=None,
        source=source,
        source_ref=None,
        requested_by=requested_by,
        kind=JobKind.REQUIREMENTS,
        profile_name=profile_name,
        budget_usd=budget_usd,
        backlog_id=bl.id,
    )
    bl.requirements_job_id = job.id
    await s.flush()
    await _emit(s, bl, "backlog.created", {"title": bl.title})
    return bl, job


async def _make_jobs(
    s: AsyncSession,
    bl: Backlog,
    app_slug: str,
    items: Sequence[RequirementItem],
    profile_name: str | None,
) -> list[Job]:
    ids = [i.id for i in items]
    if len(set(ids)) != len(ids):
        raise BacklogError("duplicate item id in backlog items")
    jobs: list[Job] = []
    for pos, item in enumerate(items, start=1):
        jobs.append(
            await enqueue_job(
                s,
                app_slug=app_slug,
                feature=render_item_feature(item),
                title=item.title,
                source="backlog",
                source_ref=f"backlog:{bl.id}:{item.id}",
                requested_by=bl.created_by,
                profile_name=profile_name,
                backlog_id=bl.id,
                backlog_pos=pos,
            )
        )
    return jobs


async def import_backlog(
    s: AsyncSession,
    *,
    app_slug: str,
    title: str,
    items: Sequence[RequirementItem],
    requested_by: str,
) -> tuple[Backlog, list[Job]]:
    bl = await _new_backlog(s, app_slug, title[:80], requested_by, BacklogStatus.RUNNING)
    jobs = await _make_jobs(s, bl, app_slug, items, None)
    bl.approved_by = requested_by
    bl.approved_at = func.now()
    await s.flush()
    await _emit(s, bl, "backlog.created", {"title": bl.title})
    await _emit(s, bl, "backlog.approved", {"items": len(jobs), "approved_by": requested_by})
    await s.refresh(bl)
    return bl, jobs


async def approve_backlog(
    s: AsyncSession, *, backlog_id: UUID, items: Sequence[RequirementItem], approved_by: str
) -> list[Job]:
    bl = await _locked(s, backlog_id)
    existing = await _feature_jobs(s, backlog_id)
    if existing:
        return existing
    if bl.status in (BacklogStatus.CANCELLED.value, BacklogStatus.DONE.value):
        ref = format_backlog_ref(await _slug(s, bl), bl.seq)
        raise BacklogError(f"backlog {ref} is {bl.status}")
    profile = None
    if bl.requirements_job_id is not None:
        profile = await s.scalar(select(Job.profile_name).where(Job.id == bl.requirements_job_id))
    jobs = await _make_jobs(s, bl, await _slug(s, bl), items, profile)
    bl.approved_by = approved_by
    bl.approved_at = func.now()
    bl.status = BacklogStatus.RUNNING.value
    bl.updated_at = func.now()
    await s.flush()
    await _emit(s, bl, "backlog.approved", {"items": len(jobs), "approved_by": approved_by})
    await s.refresh(bl)
    return jobs


async def refresh_backlog(s: AsyncSession, backlog_id: UUID) -> str:
    bl = await _locked(s, backlog_id)
    old = bl.status
    if old == BacklogStatus.CANCELLED.value:
        return old
    jobs = await _feature_jobs(s, backlog_id)
    extra: dict[str, Any] = {}
    if not jobs:
        stage = None
        if bl.requirements_job_id is not None:
            stage = await s.scalar(
                select(Job.stage)
                .where(Job.id == bl.requirements_job_id)
                .execution_options(populate_existing=True)
            )
        if stage == Stage.NEEDS_HUMAN.value:
            new = BacklogStatus.AWAITING_APPROVAL
        elif stage in (Stage.CLOSED.value, Stage.CANCELLED.value):
            new = BacklogStatus.CANCELLED
        else:
            new = BacklogStatus.DRAFTING
    else:
        settled = [j.stage == Stage.MERGED.value or j.backlog_skipped for j in jobs]
        cause = next(
            (
                j
                for j in jobs
                if not j.backlog_skipped
                and (j.stage in _BLOCKING or j.stage == Stage.CANCELLED.value)
            ),
            None,
        )
        if all(settled):
            new = BacklogStatus.DONE
        elif cause is not None:
            new = BacklogStatus.PAUSED
            extra = {"stage": cause.stage, "job": cause}
        else:
            new = BacklogStatus.RUNNING
    if new.value == old:
        return old
    bl.status = new.value
    bl.updated_at = func.now()
    await s.flush()
    if new is BacklogStatus.PAUSED:
        job = extra["job"]
        slug = await _slug(s, bl)
        await _emit(
            s,
            bl,
            "backlog.paused",
            {"blocked_job": f"{slug}:{job.seq:03d}", "stage": extra["stage"]},
        )
    elif new is BacklogStatus.RUNNING and old == BacklogStatus.PAUSED.value:
        await _emit(s, bl, "backlog.resumed")
    elif new is BacklogStatus.DONE:
        await _emit(s, bl, "backlog.done")
    elif new is BacklogStatus.CANCELLED:
        await _emit(s, bl, "backlog.cancelled")
    return new.value


async def blocked_job(s: AsyncSession, backlog_id: UUID) -> Job | None:
    status = await s.scalar(select(Backlog.status).where(Backlog.id == backlog_id))
    if status != BacklogStatus.PAUSED.value:
        return None
    for j in await _feature_jobs(s, backlog_id):
        if j.stage != Stage.MERGED.value and not j.backlog_skipped:
            return j
    return None


async def skip_blocked(s: AsyncSession, *, backlog_id: UUID) -> Job:
    bl = await _locked(s, backlog_id)
    if bl.status != BacklogStatus.PAUSED.value:
        ref = format_backlog_ref(await _slug(s, bl), bl.seq)
        raise BacklogError(f"backlog {ref} is not paused")
    job = await blocked_job(s, backlog_id)
    if job is None:
        raise BacklogError("backlog has no blocked job")
    job.backlog_skipped = True
    await s.flush()
    await refresh_backlog(s, backlog_id)
    return job


async def cancel_backlog(s: AsyncSession, *, backlog_id: UUID) -> list[Job]:
    bl = await _locked(s, backlog_id)
    if bl.status != BacklogStatus.CANCELLED.value:
        bl.status = BacklogStatus.CANCELLED.value
        bl.updated_at = func.now()
        await s.flush()
        await _emit(s, bl, "backlog.cancelled")
    rows = await s.scalars(
        select(Job)
        .where(Job.backlog_id == backlog_id)
        .order_by(Job.backlog_pos.nulls_first(), Job.created_at)
        .execution_options(populate_existing=True)
    )
    terminal = {st.value for st in TERMINAL_STAGES}
    return [j for j in rows.all() if j.stage not in terminal]
