"""Repository functions for agent runs. Each takes the caller's session and never commits."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.commands import GATE_ACTIONS
from swfactory.db.tables import (
    AgentRun,
    App,
    BudgetReservation,
    CatalogModel,
    Command,
    Job,
    JobEvent,
    ModelDecision,
)
from swfactory.domain.types import (
    DEFAULT_REVIEWERS,
    TERMINAL_STAGES,
    CostSource,
    HumanAction,
    RunStatus,
    Stage,
    Usage,
)
from swfactory.events import record_event
from swfactory.models.catalog import ModelInfo


class LeaseLost(Exception):
    """This worker no longer holds the job's lease (another claim, or the lease ran out)."""


async def holds_lease(s: AsyncSession, *, job_id: UUID, worker_id: str, lease_epoch: int) -> bool:
    """Lock the job row and say whether this worker's claim (owner and epoch) is still current."""
    row = await s.scalar(
        text(
            "SELECT id FROM app.jobs WHERE id = :id AND lease_owner = :me "
            "AND lease_epoch = :e FOR UPDATE"
        ),
        {"id": job_id, "me": worker_id, "e": lease_epoch},
    )
    return row is not None


@dataclass(frozen=True, slots=True)
class AgentRunRow:
    id: UUID
    job_id: UUID
    role: str
    round: int
    attempt: int
    head_sha: str | None
    status: str
    verdict: str | None
    artifact_path: str | None
    error: str | None
    meta: Mapping[str, Any]


def _row(r: AgentRun) -> AgentRunRow:
    return AgentRunRow(
        r.id,
        r.job_id,
        r.role,
        r.round,
        r.attempt,
        r.head_sha,
        r.status,
        r.verdict,
        r.artifact_path,
        r.error,
        dict(r.meta or {}),
    )


async def start_agent_run(
    s: AsyncSession,
    *,
    job_id: UUID,
    role: str,
    round: int,
    engine: str,
    model_alias: str | None,
    head_sha: str | None = None,
    meta: Mapping[str, Any] = {},  # noqa: B006
    runner_digest: str | None = None,
    run_id: UUID | None = None,
    status: str = RunStatus.RUNNING.value,
    decision_id: UUID | None = None,
) -> tuple[UUID, int]:
    """Insert a row as the next attempt of (job, role, round) and emit run.started.

    Runs in the caller's session (no commit). ``run_id`` is the id allocated before the budget
    reservation and the gateway key; ``decision_id`` links the model decision both ways."""
    last = await s.scalar(
        select(func.max(AgentRun.attempt)).where(
            AgentRun.job_id == job_id, AgentRun.role == str(role), AgentRun.round == round
        )
    )
    attempt = (last or 0) + 1
    row = AgentRun(
        job_id=job_id,
        role=str(role),
        round=round,
        attempt=attempt,
        head_sha=head_sha,
        engine=engine,
        model_alias=model_alias,
        status=str(status),
        meta=dict(meta),
        runner_digest=runner_digest,
        decision_id=decision_id,
    )
    if run_id is not None:
        row.id = run_id
    s.add(row)
    await s.flush()
    if decision_id is not None:
        await s.execute(
            update(ModelDecision)
            .where(ModelDecision.id == decision_id, ModelDecision.run_id.is_(None))
            .values(run_id=row.id)
        )
    await record_event(
        s,
        job_id,
        "run.started",
        {
            "run_id": row.id,
            "role": str(role),
            "round": round,
            "attempt": attempt,
            "engine": engine,
            "model": model_alias,
        },
    )
    return row.id, attempt


async def finish_agent_run(
    s: AsyncSession,
    run_id: UUID,
    *,
    status: RunStatus,
    verdict: str | None,
    artifact_path: str | None,
    usage: Usage,
    error: str | None,
    engine_version: str | None,
    used_models: tuple[str, ...] | list[str],
) -> bool:
    """Finish a running row (False when it was not running) and emit run.finished with it."""
    res = await s.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status == RunStatus.RUNNING.value)
        .values(
            status=status.value,
            verdict=verdict,
            artifact_path=artifact_path,
            error=error,
            engine_version=engine_version,
            used_models=list(used_models),
            finished_at=datetime.now(UTC),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
        )
        .returning(AgentRun.job_id, AgentRun.role, AgentRun.round, AgentRun.attempt)
    )
    found = res.first()
    if found is None:
        return False
    await record_event(
        s,
        found.job_id,
        "run.finished",
        {
            "run_id": run_id,
            "role": found.role,
            "round": found.round,
            "attempt": found.attempt,
            "status": status.value,
            "verdict": verdict,
            "artifact_path": artifact_path,
            "error": error,
        },
    )
    return True


async def lock_job(s: AsyncSession, job_id: UUID) -> None:
    """Take the job row's lock: do it before locking any of its runs (the cancel's order)."""
    await s.execute(select(Job.id).where(Job.id == job_id).with_for_update())


async def record_run_cost(
    s: AsyncSession, run_id: UUID, cost_usd: Decimal, source: CostSource
) -> bool:
    """Set the run's cost where it is still null, add it to the job's and emit cost.updated.

    True when it did; a second call for the same run changes nothing. The job row is locked
    before the run row, the order a cancel uses, so the two cannot deadlock."""
    job_id = await s.scalar(select(AgentRun.job_id).where(AgentRun.id == run_id))
    if job_id is None:
        return False
    await lock_job(s, job_id)
    res = await s.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.cost_usd.is_(None))
        .values(cost_usd=cost_usd, cost_source=source.value)
        .returning(AgentRun.job_id, AgentRun.role)
    )
    found = res.first()
    if found is None:
        return False
    total = await s.scalar(
        update(Job)
        .where(Job.id == found.job_id)
        .values(cost_usd=Job.cost_usd + cost_usd)
        .returning(Job.cost_usd)
    )
    await record_event(
        s,
        found.job_id,
        "cost.updated",
        {
            "run_id": run_id,
            "role": found.role,
            "usd": cost_usd,
            "source": source.value,
            "job_cost_usd": total,
        },
    )
    return True


async def latest_reviewer_verdicts(
    s: AsyncSession, job_id: UUID, *, before_round: int
) -> dict[str, str]:
    """Reviewer kind -> latest verdict of the rounds before ``before_round``."""
    rows = await s.execute(
        select(AgentRun.role, AgentRun.verdict)
        .where(
            AgentRun.job_id == job_id,
            AgentRun.role.like("reviewer-%"),
            AgentRun.round < before_round,
            AgentRun.verdict.is_not(None),
        )
        .order_by(AgentRun.round, AgentRun.attempt)
    )
    return {
        role.removeprefix("reviewer-"): verdict
        for role, verdict in rows.all()
        if verdict is not None
    }


async def ok_runs(s: AsyncSession, job_id: UUID, role: str, round: int) -> list[AgentRunRow]:
    rows = await s.scalars(
        select(AgentRun)
        .where(
            AgentRun.job_id == job_id,
            AgentRun.role == str(role),
            AgentRun.round == round,
            AgentRun.status == RunStatus.OK.value,
        )
        .order_by(AgentRun.attempt)
    )
    return [_row(r) for r in rows]


async def fail_stale_runs(
    s: AsyncSession, job_id: UUID, role: str, round: int
) -> list[AgentRunRow]:
    """Mark every still-running row of (job, role, round) failed / interrupted."""
    rows = (
        await s.scalars(
            update(AgentRun)
            .where(
                AgentRun.job_id == job_id,
                AgentRun.role == str(role),
                AgentRun.round == round,
                AgentRun.status == RunStatus.RUNNING.value,
            )
            .values(
                status=RunStatus.FAILED.value, error="interrupted", finished_at=datetime.now(UTC)
            )
            .returning(AgentRun)
        )
    ).all()
    out = sorted((_row(r) for r in rows), key=lambda r: r.attempt)
    for r in out:
        await record_event(
            s,
            job_id,
            "run.finished",
            {
                "run_id": r.id,
                "role": r.role,
                "round": r.round,
                "attempt": r.attempt,
                "status": "failed",
                "verdict": None,
                "artifact_path": None,
                "error": "interrupted",
            },
        )
    return out


class JobRefError(ValueError):
    """A job reference that is malformed or names no job."""


async def resolve_job_ref(s: AsyncSession, ref: str) -> Job:
    """A job by uuid or ``<app>:<seq>`` (seq with or without zero padding)."""
    ref = ref.strip()
    try:
        job = await s.get(Job, UUID(ref))
    except ValueError:
        app_slug, sep, seq = ref.rpartition(":")
        if not sep or not app_slug or not seq.isdigit():
            raise JobRefError(f"bad job reference '{ref}'") from None
        job = await s.scalar(
            select(Job)
            .join(App, App.id == Job.app_id)
            .where(App.slug == app_slug, Job.seq == int(seq))
        )
    if job is None:
        raise JobRefError(f"no such job '{ref}'")
    return job


@dataclass(frozen=True, slots=True)
class CommandRow:
    id: UUID
    job_id: UUID
    action: str
    note: str | None
    requested_by: str
    head_sha: str | None
    gate_event_id: int | None = None


_GATE = tuple(a.value for a in GATE_ACTIONS)


def _command_row(c: Command) -> CommandRow:
    return CommandRow(c.id, c.job_id, c.action, c.note, c.requested_by, c.head_sha, c.gate_event_id)


async def insert_command(
    s: AsyncSession,
    *,
    job_id: UUID,
    action: str,
    note: str | None,
    requested_by: str,
    gate_event_id: int | None = None,
) -> UUID:
    cid = await s.scalar(
        insert(Command)
        .values(
            job_id=job_id,
            action=action,
            note=note,
            requested_by=requested_by,
            gate_event_id=gate_event_id,
        )
        .returning(Command.id)
    )
    assert cid is not None
    return cid


async def next_command(s: AsyncSession, job_id: UUID) -> CommandRow | None:
    c = await s.scalar(
        select(Command)
        .where(Command.job_id == job_id, Command.consumed_at.is_(None))
        .order_by(Command.created_at, Command.id)
        .limit(1)
    )
    return None if c is None else _command_row(c)


async def pending_cancel(s: AsyncSession, job_id: UUID) -> CommandRow | None:
    """The oldest unconsumed ``cancel`` of a job, wherever it sits in the queue of commands."""
    c = await s.scalar(
        select(Command)
        .where(
            Command.job_id == job_id,
            Command.consumed_at.is_(None),
            Command.action == HumanAction.CANCEL.value,
        )
        .order_by(Command.created_at, Command.id)
        .limit(1)
    )
    return None if c is None else _command_row(c)


async def pending_gate_command(s: AsyncSession, job_id: UUID, wait_id: int) -> CommandRow | None:
    """The oldest unconsumed gate command bound to the wait ``wait_id``."""
    c = await s.scalar(
        select(Command)
        .where(
            Command.job_id == job_id,
            Command.consumed_at.is_(None),
            Command.action.in_(_GATE),
            Command.gate_event_id == wait_id,
        )
        .order_by(Command.created_at, Command.id)
        .limit(1)
    )
    return None if c is None else _command_row(c)


async def consume_command(s: AsyncSession, command_id: UUID, result: str) -> None:
    await s.execute(
        update(Command)
        .where(Command.id == command_id)
        .values(consumed_at=datetime.now(UTC), result=result)
    )


async def consume_gate_commands(
    s: AsyncSession, job_id: UUID, *, wait_id: int, answers: bool, result: str
) -> None:
    """Consume the unconsumed gate commands of a job bound to ``wait_id`` (``answers``) or not."""
    cond = (
        Command.gate_event_id == wait_id
        if answers
        else Command.gate_event_id.is_distinct_from(wait_id)
    )
    await s.execute(
        update(Command)
        .where(
            Command.job_id == job_id,
            Command.consumed_at.is_(None),
            Command.action.in_(_GATE),
            cond,
        )
        .values(consumed_at=datetime.now(UTC), result=result)
    )


async def latest_waiting(s: AsyncSession, job_id: UUID) -> tuple[int, Mapping[str, Any]] | None:
    """The id and data of the job's latest ``human.waiting`` event."""
    row = (
        await s.execute(
            select(JobEvent.id, JobEvent.data)
            .where(JobEvent.job_id == job_id, JobEvent.kind == "human.waiting")
            .order_by(JobEvent.id.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else (int(row[0]), dict(row[1] or {}))


async def command_applied(s: AsyncSession, job_id: UUID, command_id: UUID) -> bool:
    """Whether a ``human.action`` event already records this command as applied."""
    found = await s.scalar(
        select(JobEvent.id)
        .where(
            JobEvent.job_id == job_id,
            JobEvent.kind == "human.action",
            JobEvent.data["command_id"].astext == str(command_id),
        )
        .limit(1)
    )
    return found is not None


async def claim_commanded_job(
    s: AsyncSession, *, worker_id: str, lease_s: int, exclude: Collection[UUID] = ()
) -> tuple[Job, CommandRow] | None:
    """Claim a job with an unconsumed command and no live lease, whatever its app's limit.

    A ``needs_human`` job with a gate command, a ``failed`` job with a ``retry``, or a job in
    any non-terminal stage with a ``cancel`` (which goes first). Jobs backing off after a
    crash are skipped, except for a ``cancel``. Every claim bumps ``lease_epoch``. Needs no
    open transaction.
    """
    from swfactory.queue import _UPDATE  # the claim statement shared with claim_next_job

    skip = sorted(str(j) for j in exclude)
    terminal = sorted(st.value for st in TERMINAL_STAGES)
    async with s.begin():
        found = (
            await s.execute(
                text(
                    """
                    SELECT j.id AS job_id, c.id AS command_id
                      FROM app.jobs j JOIN app.commands c ON c.job_id = j.id
                     WHERE c.consumed_at IS NULL
                       AND (j.lease_until IS NULL OR j.lease_until < now())
                       AND j.id <> ALL(CAST(CAST(:exclude AS text[]) AS uuid[]))
                       AND (c.action = 'cancel' OR j.retry_after IS NULL OR j.retry_after <= now())
                       AND ((c.action = 'cancel' AND NOT (j.stage = ANY(CAST(:terminal AS text[]))))
                         OR (c.action = ANY(CAST(:gate AS text[])) AND j.stage = 'needs_human')
                         OR (c.action = 'retry' AND j.stage = 'failed'))
                     ORDER BY (c.action = 'cancel') DESC, c.created_at, c.id
                     LIMIT 1 FOR UPDATE OF j SKIP LOCKED
                    """
                ),
                {"exclude": skip, "terminal": terminal, "gate": list(_GATE)},
            )
        ).first()
        if found is None:
            return None
        job = (
            await s.execute(
                select(Job).from_statement(text(_UPDATE)),
                {"worker_id": worker_id, "lease_s": lease_s, "id": found.job_id},
            )
        ).scalar_one()
        c = await s.get(Command, found.command_id)
        assert c is not None
        return job, _command_row(c)


_TERMINAL = frozenset(st.value for st in TERMINAL_STAGES)


async def set_stage(s: AsyncSession, job_id: UUID, stage: Stage, **fields: Any) -> bool:
    """Move a job to ``stage`` (with other column ``fields``); False if it already ended.

    Emits ``job.stage`` only when the stage changes, never leaves a terminal stage, and
    refreshes the job's backlog in the same transaction.
    """
    job = await s.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise RuntimeError(f"job {job_id} not found")
    if job.stage in _TERMINAL:
        return False
    for key, value in fields.items():
        if not hasattr(Job, key):
            raise AttributeError(f"jobs has no column {key!r}")
        setattr(job, key, value)
    changed = job.stage != stage.value
    if changed:
        job.stage = stage.value
    job.updated_at = func.now()
    await s.flush()
    if changed:
        await record_event(s, job_id, "job.stage", {"stage": stage.value})
        if stage == Stage.FAILED:
            await record_event(s, job_id, "job.failed", {"error": job.error})
    if job.backlog_id is not None:
        from swfactory.backlogs import refresh_backlog

        await refresh_backlog(s, job.backlog_id)
    return True


async def record_crash(
    s: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    lease_epoch: int,
    error: str,
    max_crashes: int,
) -> bool:
    """Count a crash of the job this worker holds; True when the job is now ``failed``.

    Below ``max_crashes`` the job backs off (``retry_after``); its stage is left as it was.
    Raises LeaseLost when the claim is no longer current.
    """
    from swfactory.worker import crash_backoff_s

    if not await holds_lease(s, job_id=job_id, worker_id=worker_id, lease_epoch=lease_epoch):
        raise LeaseLost(str(job_id))
    error = error[:2000]
    count = int(
        await s.scalar(
            update(Job)
            .where(Job.id == job_id)
            .values(crash_count=Job.crash_count + 1, error=error, updated_at=func.now())
            .returning(Job.crash_count)
        )
        or 0
    )
    if count >= max_crashes:
        await set_stage(s, job_id, Stage.FAILED, error=error)
        return True
    await s.execute(
        text("UPDATE app.jobs SET retry_after = now() + make_interval(secs => :b) WHERE id = :id"),
        {"b": crash_backoff_s(count), "id": job_id},
    )
    return False


async def reset_crashes(s: AsyncSession, job_id: UUID) -> None:
    """A node completed: clear the crash count and any backoff."""
    await s.execute(
        text(
            "UPDATE app.jobs SET crash_count = 0, retry_after = NULL "
            "WHERE id = :id AND crash_count > 0"
        ),
        {"id": job_id},
    )


async def requeue_failed(s: AsyncSession, *, job_id: UUID, command_id: UUID, by: str) -> None:
    """Apply a ``retry`` to a failed job: back to ``queued`` with a clean crash count."""
    await set_stage(s, job_id, Stage.QUEUED, crash_count=0, retry_after=None)
    await record_event(
        s,
        job_id,
        "human.action",
        {"action": "retry", "by": by, "command_id": str(command_id), "status": "applied"},
    )
    await consume_command(s, command_id, "requeued")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def board(s: AsyncSession, *, app_slug: str | None = None) -> dict[str, Any]:
    """Jobs in v1's board.json shape plus app, cost and models (one query per table)."""
    q = (
        select(Job, App.slug)
        .join(App, App.id == Job.app_id)
        .order_by(Job.updated_at.desc(), Job.seq.desc())
    )
    if app_slug is not None:
        q = q.where(App.slug == app_slug)
    found = (await s.execute(q)).all()
    ids = [j.id for j, _ in found]
    # latest attempt per (job, role, round), verdict taken from the reviewer roles
    verdicts: dict[tuple[UUID, int], dict[str, tuple[int, str]]] = {}
    models: dict[UUID, dict[str, tuple[datetime, str]]] = {}
    files: dict[UUID, list[str]] = {}
    if ids:
        runs = (
            await s.execute(
                select(
                    AgentRun.job_id,
                    AgentRun.role,
                    AgentRun.round,
                    AgentRun.attempt,
                    AgentRun.verdict,
                    AgentRun.model_alias,
                    AgentRun.started_at,
                ).where(AgentRun.job_id.in_(ids))
            )
        ).all()
        best: dict[tuple[UUID, str, int], int] = {}
        for r in runs:
            if r.model_alias:
                cur = models.setdefault(r.job_id, {}).get(r.role)
                if cur is None or r.started_at >= cur[0]:
                    models[r.job_id][r.role] = (r.started_at, r.model_alias)
            if r.role.startswith("reviewer-") and r.verdict:
                kind = r.role.removeprefix("reviewer-")
                key = (r.job_id, r.round)
                prev = verdicts.setdefault(key, {}).get(kind)
                if prev is None or r.attempt >= prev[0]:
                    verdicts[key][kind] = (r.attempt, r.verdict)
            best[(r.job_id, r.role, r.round)] = max(
                best.get((r.job_id, r.role, r.round), 0), r.attempt
            )
        paths = await s.execute(
            text("SELECT job_id, path FROM app.artifacts_latest WHERE job_id = ANY(:ids)"),
            {"ids": ids},
        )
        for jid, path in paths.all():
            files.setdefault(jid, []).append(path)

    def ordered(d: dict[str, tuple[int, str]]) -> dict[str, str]:
        keys = [k for k in DEFAULT_REVIEWERS if k in d] + sorted(
            k for k in d if k not in DEFAULT_REVIEWERS
        )
        return {k: d[k][1] for k in keys}

    out: list[dict[str, Any]] = []
    for j, slug in found:
        jid = f"{j.seq:03d}-{j.slug}"
        rounds = sorted(r for (i, r) in verdicts if i == j.id)
        out.append(
            {
                "id": jid,
                "job_id": str(j.id),
                "title": j.title,
                "branch": j.branch or f"factory/{jid}",
                "stage": j.stage,
                "round": j.round,
                "reviews": ordered(verdicts.get((j.id, j.round), {})),
                "history": [
                    {"round": r, "reviews": ordered(verdicts[(j.id, r)])}
                    for r in rounds
                    if r < j.round
                ],
                "decision": j.decision,
                "files": sorted(files.get(j.id, [])),
                "updated": _iso(j.updated_at),
                "app": slug,
                "cost": f"{j.cost_usd:.6f}",
                "models": {role: m for role, (_, m) in models.get(j.id, {}).items()},
            }
        )
    return {"jobs": out}


# -- model catalog (M5.4) ----------------------------------------------------------------------


def _model_info(r: CatalogModel) -> ModelInfo:
    return ModelInfo(
        alias=r.alias,
        provider=r.provider,
        upstream_id=r.upstream_id,
        strength=r.strength or "",
        price_in_per_mtok=r.price_in_per_mtok or Decimal(0),
        price_out_per_mtok=r.price_out_per_mtok or Decimal(0),
        context_window=r.context_window,
        supports_tools=r.supports_tools,
        messages_tools_ok=r.messages_tools_ok,
        available=r.available,
        error_class=r.error_class,
        latency_ms=r.latency_ms,
        last_error=r.last_error,
    )


async def upsert_models(
    s: AsyncSession, infos: Sequence[ModelInfo], *, probed_at: datetime | None = None
) -> None:
    """Insert or update one row per alias; last_probe_at is set to probed_at (default now)."""
    when = probed_at or datetime.now(UTC)
    for i in infos:
        values = {
            "provider": i.provider,
            "upstream_id": i.upstream_id,
            "strength": i.strength,
            "price_in_per_mtok": i.price_in_per_mtok,
            "price_out_per_mtok": i.price_out_per_mtok,
            "context_window": i.context_window,
            "supports_tools": i.supports_tools,
            "messages_tools_ok": i.messages_tools_ok,
            "available": i.available,
            "error_class": i.error_class,
            "latency_ms": i.latency_ms,
            "last_probe_at": when,
            "last_error": i.last_error,
        }
        stmt = pg_insert(CatalogModel).values(alias=i.alias, **values)
        await s.execute(
            stmt.on_conflict_do_update(index_elements=[CatalogModel.alias], set_=values)
        )


async def get_model(s: AsyncSession, alias: str) -> ModelInfo | None:
    r = await s.get(CatalogModel, alias)
    return _model_info(r) if r is not None else None


async def list_models(s: AsyncSession, *, available_only: bool = False) -> list[ModelInfo]:
    q = select(CatalogModel).order_by(CatalogModel.alias)
    if available_only:
        q = q.where(CatalogModel.available.is_(True))
    return [_model_info(r) for r in (await s.scalars(q)).all()]


async def mark_model_unavailable(s: AsyncSession, alias: str, error_class: str, error: str) -> None:
    """available=false with the error; last_probe_at stays so the next due probe re-checks."""
    await s.execute(
        update(CatalogModel)
        .where(CatalogModel.alias == alias)
        .values(available=False, error_class=error_class, last_error=error)
    )


async def mark_models_not_in_definition(s: AsyncSession, keep: Collection[str]) -> None:
    await s.execute(
        update(CatalogModel)
        .where(CatalogModel.alias.not_in(list(keep)))
        .values(available=False, last_error="not in definition")
    )


async def model_probe_state(s: AsyncSession) -> dict[str, tuple[datetime | None, str | None]]:
    """alias -> (last_probe_at, error_class) for every stored row."""
    rows = await s.execute(
        select(CatalogModel.alias, CatalogModel.last_probe_at, CatalogModel.error_class)
    )
    return {a: (t, e) for a, t, e in rows.all()}


async def insert_model_decision(
    s: AsyncSession,
    *,
    job_id: UUID | None,
    role: str | None,
    round: int | None,
    mode: str,
    options: Sequence[Mapping[str, Any]],
    chosen: str | None,
    reason: str | None,
    latency_ms: int | None,
    purpose: str = "model",
) -> UUID:
    row = ModelDecision(
        job_id=job_id,
        role=role,
        round=round,
        purpose=purpose,
        mode=mode,
        options=list(options),
        answer=None,
        chosen=chosen,
        reason=reason,
        latency_ms=latency_ms,
    )
    s.add(row)
    await s.flush()
    return row.id


async def last_model_decision(s: AsyncSession, job_id: UUID, role: str) -> str | None:
    """The alias chosen by the latest model decision for (job, role); None when there is none."""
    return await s.scalar(
        select(ModelDecision.chosen)
        .where(
            ModelDecision.job_id == job_id,
            ModelDecision.role == role,
            ModelDecision.purpose == "model",
            ModelDecision.chosen.is_not(None),
        )
        .order_by(ModelDecision.created_at.desc(), ModelDecision.id.desc())
        .limit(1)
    )


# --------------------------------------------------------------------------
# Spend and budget reservations (M5.7)
# --------------------------------------------------------------------------


async def spend_for_job(s: AsyncSession, job_id: UUID) -> Decimal:
    """Money the job's agent runs have cost so far (open reservations are not included)."""
    total = await s.scalar(
        select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(AgentRun.job_id == job_id)
    )
    return Decimal(total or 0)


async def spend_since(s: AsyncSession, since: datetime, exclude_kind: str | None = None) -> Decimal:
    """Agent-run cost since ``since`` across all jobs, leaving out jobs of ``exclude_kind``."""
    q = (
        select(func.coalesce(func.sum(AgentRun.cost_usd), 0))
        .join(Job, Job.id == AgentRun.job_id)
        .where(AgentRun.started_at >= since)
    )
    if exclude_kind is not None:
        q = q.where(Job.kind != exclude_kind)
    return Decimal(await s.scalar(q) or 0)


async def spend_for_app_since(s: AsyncSession, app_slug: str, since: datetime) -> Decimal:
    q = (
        select(func.coalesce(func.sum(AgentRun.cost_usd), 0))
        .join(Job, Job.id == AgentRun.job_id)
        .join(App, App.id == Job.app_id)
        .where(App.slug == app_slug, AgentRun.started_at >= since)
    )
    return Decimal(await s.scalar(q) or 0)


async def open_reservations(
    s: AsyncSession,
    *,
    job_id: UUID | None = None,
    app_slug: str | None = None,
    since: datetime | None = None,
    exclude_kind: str | None = None,
) -> Decimal:
    """Sum of reservations not yet released, narrowed by job, app, start time and job kind."""
    q = select(func.coalesce(func.sum(BudgetReservation.amount), 0)).where(
        BudgetReservation.released_at.is_(None)
    )
    if job_id is not None:
        q = q.where(BudgetReservation.job_id == job_id)
    if since is not None:
        q = q.where(BudgetReservation.created_at >= since)
    if app_slug is not None:
        q = q.join(App, App.id == BudgetReservation.app_id).where(App.slug == app_slug)
    if exclude_kind is not None:
        q = q.join(Job, Job.id == BudgetReservation.job_id).where(Job.kind != exclude_kind)
    return Decimal(await s.scalar(q) or 0)


_ENDED_STAGES = (Stage.CANCELLED.value, Stage.CLOSED.value, Stage.MERGED.value, Stage.FAILED.value)


def _run_ended():
    """True for a run that is not running, or is running in a job that no step will clean up."""
    return or_(AgentRun.status != RunStatus.RUNNING.value, Job.stage.in_(_ENDED_STAGES))


async def runs_missing_cost(s: AsyncSession, *, ended_before: datetime, limit: int) -> list[UUID]:
    """Runs with no cost whose end (or, for a running row of an ended job, the job's end)
    is before ``ended_before``, oldest first."""
    running_orphan = and_(AgentRun.status == RunStatus.RUNNING.value, Job.stage.in_(_ENDED_STAGES))
    ended_at = case(
        (
            running_orphan,
            func.coalesce(Job.finished_at, Job.updated_at),
        ),
        else_=func.coalesce(AgentRun.finished_at, AgentRun.started_at),
    )
    q = (
        select(AgentRun.id)
        .join(Job, Job.id == AgentRun.job_id)
        .where(AgentRun.cost_usd.is_(None), _run_ended(), ended_at < ended_before)
        .order_by(ended_at, AgentRun.id)
        .limit(limit)
    )
    return list((await s.scalars(q)).all())


async def stale_reservations(
    s: AsyncSession, *, created_before: datetime, limit: int
) -> list[UUID]:
    """Run ids of open reservations older than ``created_before`` whose run has ended."""
    q = (
        select(BudgetReservation.run_id)
        .join(AgentRun, AgentRun.id == BudgetReservation.run_id)
        .join(Job, Job.id == AgentRun.job_id)
        .where(
            BudgetReservation.released_at.is_(None),
            BudgetReservation.created_at < created_before,
            _run_ended(),
        )
        .order_by(BudgetReservation.created_at, BudgetReservation.run_id)
        .limit(limit)
    )
    return list((await s.scalars(q)).all())
