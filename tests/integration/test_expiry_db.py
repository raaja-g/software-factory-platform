from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from tests.fakes.deps import make_test_deps, seed_demo
from tests.support.db_helpers import enq

from swfactory.db.repo import insert_command
from swfactory.db.tables import Job, JobEvent
from swfactory.engines.fake import FakeScript
from swfactory.expiry import expire_waiting_jobs
from swfactory.gitops.fake import FakeGitService

pytestmark = pytest.mark.db
NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
async def deps(sessions):
    await seed_demo(sessions)
    return make_test_deps(sessions, FakeScript(), git=FakeGitService())


async def record_event_at(sessions, job_id, kind, at, data=None):
    async with sessions.begin() as s:
        s.add(JobEvent(job_id=job_id, kind=kind, data=data or {}, at=at))


async def job_in(sessions, stage, waited):
    jid = (await enq(sessions, app="demo")).id
    async with sessions.begin() as s:
        await s.execute(update(Job).where(Job.id == jid).values(stage=stage))
    at = NOW - waited
    await record_event_at(sessions, jid, "job.stage", at, {"stage": stage})
    if stage == "needs_human":
        await record_event_at(sessions, jid, "human.waiting", at)
    return jid


async def stage(sessions, jid):
    async with sessions() as s:
        return await s.scalar(select(Job.stage).where(Job.id == jid))


async def event_data(sessions, jid, kind):
    async with sessions() as s:
        return await s.scalar(
            select(JobEvent.data).where(JobEvent.job_id == jid, JobEvent.kind == kind)
        )


async def test_closes_old_needs_human_and_failed_only(sessions, deps):
    old_wait = await job_in(sessions, "needs_human", timedelta(days=15))
    old_fail = await job_in(sessions, "failed", timedelta(days=15))
    fresh = await job_in(sessions, "needs_human", timedelta(days=3))
    delivered = await job_in(sessions, "delivered", timedelta(days=30))
    got = await expire_waiting_jobs(deps, now=NOW, days=14)
    assert sorted(got) == sorted([old_wait, old_fail])
    stages = [await stage(sessions, j) for j in (old_wait, old_fail, fresh, delivered)]
    assert stages == ["closed", "closed", "needs_human", "delivered"]
    assert await event_data(sessions, old_wait, "job.expired") == {
        "stage": "needs_human",
        "waited_days": 15,
    }
    assert deps.git.calls.count("keep_branch") == 2
    assert sorted(deps.sandboxes.destroyed) == sorted([old_wait, old_fail])


async def test_pending_command_blocks_expiry(sessions, deps):
    j = await job_in(sessions, "needs_human", timedelta(days=15))
    async with sessions.begin() as s:
        await insert_command(s, job_id=j, action="approve", note=None, requested_by="ann")
    assert await expire_waiting_jobs(deps, now=NOW, days=14) == []
    assert await stage(sessions, j) == "needs_human"


async def test_a_new_wait_restarts_the_clock(sessions, deps):
    j = await job_in(sessions, "needs_human", timedelta(days=20))
    await record_event_at(sessions, j, "human.waiting", NOW - timedelta(days=2))
    assert await expire_waiting_jobs(deps, now=NOW, days=14) == []


async def test_disabled_does_nothing(sessions, deps):
    await job_in(sessions, "needs_human", timedelta(days=90))
    assert await expire_waiting_jobs(deps, now=NOW, days=0) == []
