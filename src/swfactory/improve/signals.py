"""Failure signals from finished jobs, grouped by role, and the threshold/age trigger (M12.2)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.db.repo import calibration_table
from swfactory.db.tables import AgentRun, Command, Improvement, Job, Score
from swfactory.definition.models import ImproverConfig
from swfactory.definition.store import load_active
from swfactory.domain.types import Decision, JobKind, Role, Stage, Verdict
from swfactory.scoring.calibration import counted_judge_rows
from swfactory.scoring.objective import ScoreRow

__all__ = [
    "FailureSignal",
    "TriggerDecision",
    "collect_signals",
    "failures_since",
    "group_by_role",
    "last_improvement_at",
    "trigger_due",
]

DETAIL_MAX = 500
SignalKind = Literal["failed", "escalated", "score", "rework", "override", "reviewer_error"]
_REVIEWER_PREFIX = "reviewer-"
_NODE_ROLE = {
    "spec": Role.SPEC_WRITER,
    "build": Role.BUILDER,
    "verify": Role.BUILDER,
    "approve": Role.APPROVER,
}


@dataclass(frozen=True, slots=True)
class FailureSignal:
    job_id: UUID
    job_ref: str
    role: Role
    kind: SignalKind
    detail: str
    at: datetime


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    due: bool
    reason: Literal["threshold", "age"] | None
    failed_jobs: int
    oldest_at: datetime | None


def trigger_due(
    cfg: ImproverConfig, *, failed_jobs: int, oldest_at: datetime | None, now: datetime
) -> TriggerDecision:
    def out(reason: Literal["threshold", "age"] | None) -> TriggerDecision:
        return TriggerDecision(reason is not None, reason, failed_jobs, oldest_at)

    if not cfg.enabled or failed_jobs <= 0:
        return out(None)
    if failed_jobs >= cfg.failed_run_threshold:
        return out("threshold")
    if oldest_at is not None and now - oldest_at >= timedelta(days=cfg.max_failure_age_days):
        return out("age")
    return out(None)


def _job_time() -> Any:
    return func.coalesce(Job.finished_at, Job.updated_at)


def _failed_cond() -> Any:
    return or_(
        Job.stage == Stage.FAILED.value,
        Job.decision == Decision.ESCALATE.value,
        Job.gate_reason.is_not(None),
    )


def _window(since: datetime | None) -> list[Any]:
    conds: list[Any] = [Job.kind == JobKind.NORMAL.value]
    if since is not None:
        conds.append(_job_time() > since)
    return conds


async def last_improvement_at(s: AsyncSession) -> datetime | None:
    return await s.scalar(select(func.max(Improvement.created_at)))


async def failures_since(s: AsyncSession, since: datetime | None) -> tuple[int, datetime | None]:
    row = (
        await s.execute(
            select(func.count(), func.min(_job_time())).where(*_window(since), _failed_cond())
        )
    ).one()
    return int(row[0]), row[1]


def _clip(text: str | None) -> str:
    return (text or "")[:DETAIL_MAX]


def _role(value: str) -> Role | None:
    try:
        return Role(value)
    except ValueError:
        return None


def _is_reviewer(role: Role) -> bool:
    return role.value.startswith(_REVIEWER_PREFIX)


def _failed_roles(job: Job, runs: Sequence[AgentRun]) -> list[Role]:
    reason = job.gate_reason or ""
    if reason.startswith("step failed:"):
        node = reason.split(":", 1)[1].strip()
        if node == "review":
            out: list[Role] = []
            for r in runs:
                role = _role(r.role)
                if role and _is_reviewer(role) and r.status == "failed" and role not in out:
                    out.append(role)
            return out
        role = _NODE_ROLE.get(node)
        return [role] if role else []
    if job.stage == Stage.FAILED.value and not reason and runs:
        last = max(runs, key=lambda r: (r.started_at, r.round))
        role = _role(last.role)
        if role and role not in (Role.JUDGE, Role.IMPROVER, Role.TRIAGE):
            return [role]
    return []


async def collect_signals(
    s: AsyncSession, *, since: datetime | None, per_role_limit: int = 50
) -> list[FailureSignal]:
    jobs = list(
        await s.scalars(select(Job).where(*_window(since)).order_by(Job.created_at, Job.id))
    )
    if not jobs:
        return []
    ids = [j.id for j in jobs]
    by_id = {j.id: j for j in jobs}
    runs_by_job: dict[UUID, list[AgentRun]] = defaultdict(list)
    for r in await s.scalars(select(AgentRun).where(AgentRun.job_id.in_(ids))):
        runs_by_job[r.job_id].append(r)
    run_by_id = {r.id: r for rs in runs_by_job.values() for r in rs}

    def at(j: Job) -> datetime:
        return j.finished_at or j.updated_at

    def ref(j: Job) -> str:
        return j.slug

    out: list[FailureSignal] = []

    def add(j: Job, role: Role, kind: SignalKind, detail: str | None) -> None:
        out.append(FailureSignal(j.id, ref(j), role, kind, _clip(detail), at(j)))

    for j in jobs:
        runs = runs_by_job.get(j.id, [])
        failed = j.stage == Stage.FAILED.value or (j.gate_reason or "").startswith("step failed")
        if failed:
            for role in _failed_roles(j, runs):
                add(j, role, "failed", j.gate_reason or j.error)
        for r in runs:
            role = _role(r.role)
            if r.verdict == Verdict.ERROR.value and role and _is_reviewer(role):
                add(j, role, "reviewer_error", r.error or "reviewer verdict ERROR")
        if j.decision == Decision.ESCALATE.value:
            rev = [
                (r, _role(r.role))
                for r in runs
                if r.role.startswith(_REVIEWER_PREFIX) and r.verdict is not None
            ]
            last_round = max((r.round for r, _ in rev), default=None)
            chosen: list[Role] = []
            for r, role in rev:
                if (
                    r.round == last_round
                    and r.verdict == Verdict.CHANGES.value
                    and role
                    and role not in chosen
                ):
                    chosen.append(role)
            for role in chosen or [Role.APPROVER]:
                add(j, role, "escalated", j.gate_reason or "decision ESCALATE")

    for c in await s.scalars(
        select(Command)
        .where(Command.job_id.in_(ids), Command.action.in_(("rework", "approve")))
        .order_by(Command.created_at, Command.id)
    ):
        j = by_id[c.job_id]
        if c.action == "rework" and c.consumed_at is not None:
            add(j, Role.BUILDER, "rework", c.note)
        elif c.action == "approve" and j.decision == Decision.ESCALATE.value:
            add(j, Role.APPROVER, "override", c.note or "human approved an escalation")

    scores = list(
        await s.scalars(
            select(Score)
            .where(Score.job_id.in_(ids), Score.passed.is_(False))
            .order_by(Score.created_at, Score.id)
        )
    )
    if scores:
        active = await load_active(s)
        scorers = {} if active is None else active[1].scorers
        table = await calibration_table(s, scorers)
        rows = {
            sc.id: ScoreRow(
                sc.scorer,
                sc.label,
                sc.score,
                sc.passed,
                sc.reason or "",
                sc.run_id,
                sc.judge_model,
                sc.cost_usd,
                sc.rubric_sha,
                sc.judge_sha,
            )
            for sc in scores
        }
        judge_rows = [r for sc in scores if not sc.scorer.startswith("obj.") for r in [rows[sc.id]]]
        kept = {id(r) for r in counted_judge_rows(judge_rows, table)}
        for sc in scores:
            row = rows[sc.id]
            if not sc.scorer.startswith("obj.") and id(row) not in kept:
                continue
            roles: list[Role] = []
            run = run_by_id.get(sc.run_id) if sc.run_id else None
            if run is not None:
                role = _role(run.role)
                roles = [role] if role else []
            elif sc.run_id is None and sc.scorer in scorers:
                roles = [r for r in map(_role, scorers[sc.scorer].applies_to) if r]
            for role in roles:
                add(by_id[sc.job_id], role, "score", f"{sc.scorer}: {sc.reason or sc.label}")

    out.sort(key=lambda x: (x.at, str(x.job_id)), reverse=True)
    per: dict[Role, int] = defaultdict(int)
    kept_out: list[FailureSignal] = []
    for sig in out:
        if per[sig.role] < per_role_limit:
            per[sig.role] += 1
            kept_out.append(sig)
    return kept_out


def group_by_role(signals: Sequence[FailureSignal]) -> list[tuple[Role, list[FailureSignal]]]:
    groups: dict[Role, list[FailureSignal]] = defaultdict(list)
    for sig in signals:
        groups[sig.role].append(sig)
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0].value))
