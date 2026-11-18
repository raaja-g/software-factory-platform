import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.support.db_helpers import activate, claim, enq, items, put, running, status
from tests.support.db_helpers import job_events as _job_events

from swfactory.backlogs import (
    BacklogError,
    RequirementItem,
    approve_backlog,
    cancel_backlog,
    create_backlog,
    import_backlog,
    skip_blocked,
)
from swfactory.queue import enqueue_job

pytestmark = pytest.mark.db


@pytest.fixture
def job_events(sessions):
    async def _f(kind):
        return await _job_events(sessions, kind)

    return _f


async def test_create_backlog_makes_a_drafting_backlog_and_a_requirements_job(sessions, job_events):
    await activate(sessions, apps={"demo": {}})
    async with sessions.begin() as s:
        bl, job = await create_backlog(
            s, app_slug="demo", requirement="Build a todo app", requested_by="ann"
        )
    assert (bl.seq, bl.status, bl.created_by, bl.requirements_job_id) == (
        1,
        "drafting",
        "ann",
        job.id,
    )
    assert (job.kind, job.backlog_id, job.backlog_pos, job.feature, job.stage) == (
        "requirements",
        bl.id,
        None,
        "Build a todo app",
        "queued",
    )
    assert await job_events("backlog.created") == [
        {"backlog_id": str(bl.id), "ref": "demo:b001", "app": "demo", "title": "Build a todo app"}
    ]


async def test_approve_creates_the_jobs_in_order_once(sessions, job_events):
    bl, jobs = await running(sessions, "a", "b", "c")
    assert [(j.backlog_pos, j.stage, j.title, j.kind) for j in jobs] == [
        (1, "queued", "Item a", "normal"),
        (2, "queued", "Item b", "normal"),
        (3, "queued", "Item c", "normal"),
    ]
    assert "## Acceptance criteria\n- [ ] b works" in jobs[1].feature
    assert {j.source_ref for j in jobs} == {f"backlog:{bl.id}:{i}" for i in "abc"}
    async with sessions.begin() as s:
        again = await approve_backlog(
            s, backlog_id=bl.id, items=items("a", "b", "c"), approved_by="ann"
        )
    assert [j.id for j in again] == [j.id for j in jobs]
    assert await status(sessions, bl) == "running"
    assert await job_events("backlog.approved") == [
        {
            "backlog_id": str(bl.id),
            "ref": "demo:b001",
            "app": "demo",
            "items": 3,
            "approved_by": "ann",
        }
    ]


async def test_approve_refuses_a_cancelled_backlog(sessions):
    await activate(sessions, apps={"demo": {}})
    async with sessions.begin() as s:
        bl, _ = await create_backlog(s, app_slug="demo", requirement="x", requested_by="ann")
    async with sessions.begin() as s:
        await cancel_backlog(s, backlog_id=bl.id)
    with pytest.raises(BacklogError, match="cancelled"):
        async with sessions.begin() as s:
            await approve_backlog(s, backlog_id=bl.id, items=items("a"), approved_by="ann")


async def test_approve_is_one_transaction(sessions):
    await activate(sessions, apps={"demo": {}})
    async with sessions.begin() as s:
        bl, _ = await create_backlog(s, app_slug="demo", requirement="x", requested_by="ann")
    bad = [*items("a"), RequirementItem("a", "dup", "dup", (), ())]
    with pytest.raises(Exception):  # noqa: B017, PT011
        async with sessions.begin() as s:
            await approve_backlog(s, backlog_id=bl.id, items=bad, approved_by="ann")
    async with sessions() as s:
        n = await s.scalar(text("select count(*) from app.jobs where backlog_pos is not null"))
        assert n == 0
    assert await status(sessions, bl) == "drafting"


async def test_jobs_are_claimed_in_backlog_order(sessions):
    bl, jobs = await running(sessions, "a", "b", "c")
    assert (await claim(sessions, "w1")).id == jobs[0].id
    assert await claim(sessions, "w2") is None
    await put(sessions, jobs[0].id, "merged")
    assert (await claim(sessions, "w3")).id == jobs[1].id
    assert await claim(sessions, "w4") is None


async def test_a_closed_predecessor_pauses_the_backlog(sessions, job_events):
    bl, jobs = await running(sessions, "a", "b")
    await put(sessions, jobs[0].id, "closed")
    assert await status(sessions, bl) == "paused"
    assert await claim(sessions, "w1") is None
    assert await job_events("backlog.paused") == [
        {
            "backlog_id": str(bl.id),
            "ref": "demo:b001",
            "app": "demo",
            "blocked_job": "demo:001",
            "stage": "closed",
        }
    ]


@pytest.mark.parametrize("stage", ["failed", "cancelled", "needs_human"])
async def test_failed_cancelled_or_waiting_predecessor_pauses(sessions, stage):
    bl, jobs = await running(sessions, "a", "b")
    await put(sessions, jobs[0].id, stage)
    assert await status(sessions, bl) == "paused"


async def test_the_backlog_continues_when_the_blocker_merges(sessions, job_events):
    bl, jobs = await running(sessions, "a", "b")
    await put(sessions, jobs[0].id, "needs_human")
    assert await status(sessions, bl) == "paused"
    await put(sessions, jobs[0].id, "merged")
    assert await status(sessions, bl) == "running"
    assert (await claim(sessions, "w1")).id == jobs[1].id
    assert [e["ref"] for e in await job_events("backlog.resumed")] == ["demo:b001"]


async def test_skip_lets_the_next_job_go(sessions):
    bl, jobs = await running(sessions, "a", "b")
    await put(sessions, jobs[0].id, "closed")
    async with sessions.begin() as s:
        skipped = await skip_blocked(s, backlog_id=bl.id)
    assert skipped.id == jobs[0].id
    assert await status(sessions, bl) == "running"
    assert (await claim(sessions, "w1")).id == jobs[1].id


async def test_skip_needs_a_paused_backlog(sessions):
    bl, _ = await running(sessions, "a", "b")
    with pytest.raises(BacklogError, match="not paused"):
        async with sessions.begin() as s:
            await skip_blocked(s, backlog_id=bl.id)


async def test_cancel_returns_every_unmerged_job(sessions, job_events):
    bl, jobs = await running(sessions, "a", "b", "c")
    await put(sessions, jobs[0].id, "merged")
    async with sessions.begin() as s:
        todo = await cancel_backlog(s, backlog_id=bl.id)
    assert {j.id for j in todo} == {jobs[1].id, jobs[2].id}
    assert await status(sessions, bl) == "cancelled"
    assert await claim(sessions, "w1") is None
    assert len(await job_events("backlog.cancelled")) == 1


async def test_done_when_every_job_is_merged_or_skipped(sessions, job_events):
    bl, jobs = await running(sessions, "a", "b")
    await put(sessions, jobs[0].id, "merged")
    assert await status(sessions, bl) == "running"
    await put(sessions, jobs[1].id, "merged")
    assert await status(sessions, bl) == "done"
    assert len(await job_events("backlog.done")) == 1


async def test_requirements_job_stage_drives_the_status(sessions, job_events):
    await activate(sessions, apps={"demo": {}})
    async with sessions.begin() as s:
        bl, req = await create_backlog(s, app_slug="demo", requirement="x", requested_by="ann")
    await put(sessions, req.id, "needs_human")
    assert await status(sessions, bl) == "awaiting_approval"
    await put(sessions, req.id, "spec")
    assert await status(sessions, bl) == "drafting"
    await put(sessions, req.id, "closed")
    assert await status(sessions, bl) == "cancelled"
    assert len(await job_events("backlog.cancelled")) == 1


async def test_a_requirements_job_counts_toward_the_app_slot(sessions):
    await activate(sessions, apps={"demo": {}})
    async with sessions.begin() as s:
        _, req = await create_backlog(s, app_slug="demo", requirement="x", requested_by="ann")
    await enq(sessions, app="demo", feature="another")
    first = await claim(sessions, "w1")
    assert (first.id, first.kind) == (req.id, "requirements")
    assert await claim(sessions, "w2") is None


async def test_import_backlog_is_running_with_no_requirements_job(sessions):
    await activate(sessions, apps={"demo": {}})
    async with sessions.begin() as s:
        bl, jobs = await import_backlog(
            s, app_slug="demo", title="v1", items=items("1", "2"), requested_by="ann"
        )
    assert (bl.status, bl.requirements_job_id, [j.backlog_pos for j in jobs]) == (
        "running",
        None,
        [1, 2],
    )
    assert (await claim(sessions, "w1")).id == jobs[0].id


async def test_a_position_is_unique_within_a_backlog(sessions):
    bl, _ = await running(sessions, "a")
    with pytest.raises(IntegrityError):
        async with sessions.begin() as s:
            await enqueue_job(
                s,
                app_slug="demo",
                feature="dup",
                title=None,
                source="backlog",
                source_ref="x",
                requested_by="me",
                backlog_id=bl.id,
                backlog_pos=1,
            )


async def test_backlog_pos_needs_backlog_id(sessions):
    await activate(sessions, apps={"demo": {}})
    with pytest.raises(ValueError, match="backlog_pos needs backlog_id"):
        async with sessions.begin() as s:
            await enqueue_job(
                s,
                app_slug="demo",
                feature="x",
                title=None,
                source="cli",
                source_ref=None,
                requested_by="me",
                backlog_pos=1,
            )
