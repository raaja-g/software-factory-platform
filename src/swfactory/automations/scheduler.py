"""Cron schedules: at most one job per slot, UTC, no backfill (only the latest missed slot)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from croniter import croniter
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.db.tables import AutomationFire, Job
from swfactory.definition.models import AutomationDef
from swfactory.definition.store import NoActiveDefinition
from swfactory.domain.types import TERMINAL_STAGES
from swfactory.queue import enqueue_job

log = structlog.get_logger(__name__)

SCHEDULER_TICK_S = 30.0


def due_fire(cron: str, *, last: datetime | None, now: datetime) -> datetime | None:
    """The latest slot at or before ``now`` that is after ``last``, or None (UTC)."""
    now = now.astimezone(UTC)
    slot = croniter(cron, now + timedelta(seconds=1)).get_prev(datetime)
    slot = slot.astimezone(UTC) if slot.tzinfo else slot.replace(tzinfo=UTC)
    if last is not None and slot <= last.astimezone(UTC):
        return None
    return slot


def _escape_like(v: str) -> str:
    return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def automation_block_reason(
    s: AsyncSession,
    *,
    name: str,
    auto: AutomationDef,
    requested_by: str,  # noqa: ARG001 - every hook of the automation counts, whoever asked
    now: datetime,
) -> str | None:
    """None, ``"max_open_jobs"`` or ``"min_interval_s"``. Takes the automation's advisory lock."""
    await s.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('automation:' || :n))"), {"n": name}
    )
    mine = (Job.requested_by == f"schedule:{name}") | Job.requested_by.like(
        f"webhook:%/{_escape_like(name)}", escape="\\"
    )
    terminal = [st.value for st in TERMINAL_STAGES]
    open_count = await s.scalar(
        select(func.count()).select_from(Job).where(mine, Job.stage.not_in(terminal))
    )
    if (open_count or 0) >= auto.max_open_jobs:
        return "max_open_jobs"
    if auto.min_interval_s > 0:
        newest = await s.scalar(select(func.max(Job.created_at)).where(mine))
        if newest is not None and now - newest < timedelta(seconds=auto.min_interval_s):
            return "min_interval_s"
    return None


class Scheduler:
    def __init__(self, sessions: Any, definitions: Any) -> None:
        self.sessions = sessions
        self.definitions = definitions

    async def tick(self, now: datetime) -> list[UUID]:
        try:
            _, defn = await self.definitions.active()
        except NoActiveDefinition:
            return []
        made: list[UUID] = []
        for name, auto in defn.automations.items():
            if not auto.enabled or auto.trigger.provider != "schedule":
                continue
            try:
                job_id = await self._fire(name, auto, now)
            except Exception as exc:  # the fire row rolled back: the next tick tries again
                log.warning("automation.fire_failed", name=name, error=repr(exc))
                continue
            if job_id is not None:
                made.append(job_id)
        return made

    async def _fire(self, name: str, auto: AutomationDef, now: datetime) -> UUID | None:
        cron = str(auto.trigger.filter["cron"])
        async with self.sessions.begin() as s:
            await s.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('automation:' || :n))"), {"n": name}
            )
            last = await s.scalar(
                select(func.max(AutomationFire.fire_at)).where(AutomationFire.automation == name)
            )
            due = due_fire(cron, last=last, now=now)
            if due is None:
                return None
            inserted = await s.scalar(
                insert(AutomationFire)
                .values(automation=name, fire_at=due)
                .on_conflict_do_nothing()
                .returning(AutomationFire.fire_at)
            )
            if inserted is None:
                return None
            if last is None:  # new or renamed: first runs at its next slot
                return None
            reason = await automation_block_reason(
                s, name=name, auto=auto, requested_by=f"schedule:{name}", now=now
            )
            if reason is not None:
                log.info("automation.skipped", name=name, reason=reason)
                return None
            job = await enqueue_job(
                s,
                app_slug=auto.app,
                feature=auto.feature_template,
                title=None,
                source="schedule",
                source_ref=f"{name}:{due.isoformat()}",
                requested_by=f"schedule:{name}",
                profile_name=auto.profile,
            )
            await s.execute(
                text(
                    "UPDATE app.automation_fires SET job_id = :j "
                    "WHERE automation = :n AND fire_at = :t"
                ),
                {"j": job.id, "n": name, "t": due},
            )
            return job.id
