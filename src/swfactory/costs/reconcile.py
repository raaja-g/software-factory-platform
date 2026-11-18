"""Spend reconciliation: fill missing run costs and release orphaned reservations (M5.9)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import select, text, update

from swfactory.db.repo import record_run_cost, runs_missing_cost, stale_reservations
from swfactory.db.tables import AgentRun, Job
from swfactory.domain.types import CostSource, RunStatus
from swfactory.gateway.render import SPEND_BATCH_S
from swfactory.graph.deps import Deps

log = structlog.get_logger(__name__)

RESERVATION_SWEEP_AFTER = timedelta(minutes=10)
_ENDED = ("cancelled", "closed", "merged", "failed")


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    costs_filled: int
    reservations_released: int


async def _spend(deps: Deps, run_id: UUID) -> Decimal | None:
    try:
        return await deps.gateway.run_spend(run_id)
    except Exception as exc:
        log.warning("reconcile.spend_unreadable", run_id=str(run_id), error=repr(exc))
        return None


async def _finish_orphan(deps: Deps, run_id: UUID, now: datetime) -> bool:
    """Revoke the key of a running row of an ended job and finish it as failed."""
    async with deps.sessions() as s:
        row = (
            await s.execute(
                select(AgentRun.status, Job.stage)
                .join(Job, Job.id == AgentRun.job_id)
                .where(AgentRun.id == run_id)
            )
        ).first()
    if row is None:
        return False
    if row.status != RunStatus.RUNNING.value or row.stage not in _ENDED:
        return True
    try:
        await deps.gateway.revoke_run(run_id)
    except Exception as exc:
        log.warning("reconcile.revoke_failed", run_id=str(run_id), error=repr(exc))
        return False
    async with deps.sessions() as s, s.begin():
        await s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == RunStatus.RUNNING.value)
            .values(status=RunStatus.FAILED.value, error="job ended", finished_at=now)
        )
    return True


async def _cost_of(deps: Deps, run_id: UUID) -> Decimal | None:
    """The run's recorded cost, else the gateway's (recorded first); None when unknown."""
    async with deps.sessions() as s:
        cost = await s.scalar(select(AgentRun.cost_usd).where(AgentRun.id == run_id))
    if cost is not None:
        return Decimal(cost)
    spend = await _spend(deps, run_id)
    if spend is None:
        return None
    async with deps.sessions() as s, s.begin():
        await record_run_cost(s, run_id, spend, CostSource.GATEWAY)
        stored = await s.scalar(select(AgentRun.cost_usd).where(AgentRun.id == run_id))
    return Decimal(stored) if stored is not None else spend


async def reconcile_run_costs(deps: Deps, *, now: datetime, limit: int = 100) -> ReconcileResult:
    filled = 0
    async with deps.sessions() as s:
        missing = await runs_missing_cost(
            s, ended_before=now - timedelta(seconds=SPEND_BATCH_S + 1), limit=limit
        )
    for run_id in missing:
        try:
            if not await _finish_orphan(deps, run_id, now):
                continue
            spend = await _spend(deps, run_id)
            if spend is None:
                continue
            async with deps.sessions() as s, s.begin():
                if await record_run_cost(s, run_id, spend, CostSource.GATEWAY):
                    filled += 1
        except Exception as exc:
            log.warning("reconcile.cost_failed", run_id=str(run_id), error=repr(exc))
    released = 0
    async with deps.sessions() as s:
        stale = await stale_reservations(
            s, created_before=now - RESERVATION_SWEEP_AFTER, limit=limit
        )
    for run_id in stale:
        try:
            cost = await _cost_of(deps, run_id)
            if cost is None:
                continue
            await deps.budgets.release_run(run_id, cost)
            released += 1
        except Exception as exc:
            log.warning("reconcile.release_failed", run_id=str(run_id), error=repr(exc))
    return ReconcileResult(filled, released)


async def reconcile_tick(deps: Deps, *, now: datetime) -> ReconcileResult | None:
    """One reconcile under an advisory lock; None when another worker holds it."""
    async with deps.sessions() as s, s.begin():
        got = await s.scalar(text("select pg_try_advisory_xact_lock(hashtext('reconcile_costs'))"))
        if not got:
            return None
        return await reconcile_run_costs(deps, now=now)


__all__ = [
    "RESERVATION_SWEEP_AFTER",
    "ReconcileResult",
    "reconcile_run_costs",
    "reconcile_tick",
]
