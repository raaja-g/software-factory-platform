"""Worker heartbeats and the stuck-job detector (§22 Health)."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.domain.types import ACTIVE_STAGES
from swfactory.events import EventBus

STUCK_AFTER = timedelta(minutes=30)
STUCK_LOCK_KEY = 7_009_001  # fixed advisory-lock key: one detector at a time
DETECT_EVERY_S = 60


async def beat(s: AsyncSession, *, worker_id: str, jobs: int, version: str) -> None:
    """Record that a worker is alive (upsert) and commit."""
    await s.execute(
        text(
            "INSERT INTO app.worker_heartbeats (worker_id, at, jobs, version) "
            "VALUES (:w, now(), :j, :v) "
            "ON CONFLICT (worker_id) DO UPDATE SET at = now(), jobs = :j, version = :v"
        ),
        {"w": worker_id, "j": jobs, "v": version},
    )
    await s.commit()


async def live_workers(s: AsyncSession, *, within_s: int) -> int:
    n = await s.scalar(
        text(
            "SELECT count(*) FROM app.worker_heartbeats "
            "WHERE at > now() - make_interval(secs => :w)"
        ),
        {"w": within_s},
    )
    return int(n or 0)


def is_stuck(*, last_event_at: datetime, last_event_kind: str, now: datetime) -> bool:
    return last_event_kind != "job.stuck" and now - last_event_at > STUCK_AFTER


async def detect_stuck(s: AsyncSession, bus: EventBus, *, now: datetime) -> list[UUID]:
    """Emit job.stuck once per stall for active jobs with no event for STUCK_AFTER."""
    async with s.begin():
        got = await s.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": STUCK_LOCK_KEY})
        if not got:
            return []
        rows = (
            await s.execute(
                text(
                    "SELECT j.id, e.at, e.kind FROM app.jobs j "
                    "JOIN (SELECT DISTINCT ON (job_id) job_id, at, kind FROM app.job_events "
                    "      WHERE job_id IS NOT NULL ORDER BY job_id, id DESC) e "
                    "ON e.job_id = j.id WHERE j.stage = ANY(:stages) ORDER BY j.id"
                ),
                {"stages": sorted(st.value for st in ACTIVE_STAGES)},
            )
        ).all()
        stuck: list[UUID] = []
        for job_id, at, kind in rows:
            if is_stuck(last_event_at=at, last_event_kind=kind, now=now):
                await bus.emit(
                    job_id,
                    "job.stuck",
                    {
                        "last_event_at": at.isoformat(),
                        "last_event_kind": kind,
                        "minutes": int((now - at).total_seconds() // 60),
                    },
                )
                stuck.append(job_id)
        return stuck
