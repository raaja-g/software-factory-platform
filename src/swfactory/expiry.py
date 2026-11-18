"""Close jobs that wait too long for a person, so their volumes are freed."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import func, or_, select, text

from swfactory.db.repo import set_stage
from swfactory.db.tables import Command, Job, JobEvent
from swfactory.domain.types import Stage
from swfactory.events import record_event
from swfactory.graph.steps import keep_job_branch

if TYPE_CHECKING:
    from swfactory.graph.deps import Deps

log = structlog.get_logger(__name__)

EXPIRY_WARNING = timedelta(days=2)
EXPIRY_TICK_S = 3600
EXPIRY_LOCK_KEY = 0x5F_E8_71_9A_C0_DE  # fixed advisory-lock key for the expiry tick
_WAITING = (Stage.NEEDS_HUMAN.value, Stage.FAILED.value)


def waiting_expired(since: datetime, now: datetime, days: int) -> bool:
    return days > 0 and now - since >= timedelta(days=days)


def expiry_warning_due(since: datetime, now: datetime, days: int) -> bool:
    """True in the last two days before the job would expire."""
    if days <= 2:
        return False
    age = now - since
    return timedelta(days=days) - EXPIRY_WARNING <= age < timedelta(days=days)


async def expire_waiting_jobs(deps: Deps, *, now: datetime, days: int) -> list[UUID]:
    """Close needs_human and failed jobs nobody has touched for ``days`` days."""
    if days == 0:
        return []
    closed: list[UUID] = []
    async with deps.sessions() as s, s.begin():
        got = await s.scalar(text("select pg_try_advisory_xact_lock(:k)"), {"k": EXPIRY_LOCK_KEY})
        if not got:
            return []
        pending = select(Command.id).where(Command.job_id == Job.id, Command.consumed_at.is_(None))
        since_q = (
            select(func.max(JobEvent.at))
            .where(JobEvent.job_id == Job.id, JobEvent.kind.in_(("job.stage", "human.waiting")))
            .scalar_subquery()
        )
        rows = (
            await s.execute(
                select(Job.id, since_q)
                .where(
                    Job.stage.in_(_WAITING),
                    or_(Job.lease_owner.is_(None), Job.lease_until < now),
                    ~pending.exists(),
                )
                .order_by(Job.created_at)
            )
        ).all()
        for job_id, since in rows:
            if since is None or not waiting_expired(since, now, days):
                continue
            job = await s.scalar(
                select(Job).where(Job.id == job_id).with_for_update(skip_locked=True)
            )
            if job is None or job.stage not in _WAITING:
                continue
            old_stage = job.stage
            await keep_job_branch(deps, job_id)  # never raises
            try:
                await deps.sandboxes.destroy(job_id)
            except Exception as exc:
                log.warning("job.expiry_destroy_failed", job_id=str(job_id), error=repr(exc))
            await set_stage(s, job_id, Stage.CLOSED, finished_at=now)
            await record_event(
                s, job_id, "job.expired", {"stage": old_stage, "waited_days": (now - since).days}
            )
            closed.append(job_id)
    return closed
