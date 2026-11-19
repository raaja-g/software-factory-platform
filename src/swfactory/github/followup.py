"""Follow-up jobs on delivered PRs (M8.6b): send a delivered job back on its own PR branch."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.commands import CommandRefused
from swfactory.db.tables import IssueIntake, Job
from swfactory.domain.types import JobKind, Stage
from swfactory.events import record_event
from swfactory.queue import enqueue_job

REFUSED = "only a delivered job with an open PR can get a follow-up"


async def start_follow_up(s: AsyncSession, *, job_id: UUID, note: str, requested_by: str) -> Job:
    """Queue a job on the old job's PR branch and close the old one; in the caller's transaction."""
    old = (
        await s.execute(text("SELECT id FROM app.jobs WHERE id = :i FOR UPDATE"), {"i": job_id})
    ).first()
    job = None if old is None else await s.get(Job, job_id, populate_existing=True)
    if (
        job is None
        or job.kind != JobKind.NORMAL.value
        or job.stage != Stage.DELIVERED.value
        or not job.pr_url
    ):
        raise CommandRefused(REFUSED)
    slug = await s.scalar(text("SELECT slug FROM app.apps WHERE id = :a"), {"a": job.app_id})
    feature = f"{job.feature}\n\nFollow-up requested by {requested_by} on {job.pr_url}:\n{note}"
    new = await enqueue_job(
        s,
        app_slug=str(slug),
        feature=feature,
        title=f"Follow-up: {job.title}"[:120],
        source=job.source,
        source_ref=job.source_ref,
        requested_by=requested_by,
        profile_name=job.profile_name,
        follow_up_of=job.id,
        branch=job.branch,
        base_sha=job.base_sha,
    )
    job.stage = Stage.CLOSED.value
    job.finished_at = datetime.now(UTC)
    await s.flush()
    await record_event(s, job.id, "job.stage", {"stage": Stage.CLOSED.value})
    await record_event(s, job.id, "job.superseded", {"by": str(new.id)})
    await record_event(
        s,
        job.id,
        "human.action",
        {"action": "follow_up", "by": requested_by, "status": "requested"},
    )
    await s.execute(update(IssueIntake).where(IssueIntake.job_id == job.id).values(job_id=new.id))
    return new
