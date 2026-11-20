from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, update
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.db.tables import ScoreRequest
from swfactory.scoring.requests import (
    STALE_RUNNING_S,
    create_score_request,
    get_score_request,
    score_request_tick,
)
from swfactory.scoring.service import ScoreSummary

pytestmark = pytest.mark.db


class FakeScoring:
    def __init__(self, raises: Exception | None = None):
        self.calls: list[tuple] = []
        self.raises = raises

    async def score_job(self, deps, job_id, *, force=False, only=None):
        self.calls.append((job_id, force, only))
        if self.raises is not None:
            raise self.raises
        return ScoreSummary(job_id, "good", 9, 1, ("scope-creep: not sampled",), ("e",))


def deps_for(sessions, scoring) -> Any:
    return SimpleNamespace(sessions=sessions, scoring=scoring)


async def make_request(sessions, job_id, **kw):
    async with sessions.begin() as s:
        return await create_score_request(
            s,
            job_id=job_id,
            scorers=kw.get("scorers"),
            force=kw.get("force", False),
            requested_by="me",
        )


async def row_of(sessions, rid) -> ScoreRequest:
    async with sessions() as s:
        row = await get_score_request(s, rid)
    assert row is not None
    return row


async def test_claim_complete_stores_the_summary(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    rid = await make_request(sessions, job.id, scorers=["merge-readiness"], force=True)
    assert (await row_of(sessions, rid)).status == "pending"
    scoring = FakeScoring()
    assert await score_request_tick(deps_for(sessions, scoring), "w1") is True
    assert scoring.calls == [(job.id, True, ["merge-readiness"])]
    row = await row_of(sessions, rid)
    assert row.status == "done" and row.detail is None
    assert row.summary == {
        "job_id": str(job.id),
        "quality": "good",
        "rows": 9,
        "judges_run": 1,
        "judges_skipped": ["scope-creep: not sampled"],
        "errors": ["e"],
    }
    assert await score_request_tick(deps_for(sessions, scoring), "w1") is False  # nothing left


async def test_exception_marks_failed_and_never_raises(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    rid = await make_request(sessions, job.id)
    scoring = FakeScoring(raises=RuntimeError("boom"))
    assert await score_request_tick(deps_for(sessions, scoring), "w1") is True
    row = await row_of(sessions, rid)
    assert row.status == "failed" and "boom" in (row.detail or "") and row.summary is None


async def test_running_rows_are_left_alone_until_stale(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    rid = await make_request(sessions, job.id)
    async with sessions.begin() as s:
        await s.execute(update(ScoreRequest).values(status="running"))
    scoring = FakeScoring()
    assert await score_request_tick(deps_for(sessions, scoring), "w1") is False
    assert scoring.calls == []
    later = datetime.now(UTC) + timedelta(seconds=STALE_RUNNING_S - 60)
    assert await score_request_tick(deps_for(sessions, scoring), "w1", now=later) is False
    assert (await row_of(sessions, rid)).status == "running"


async def test_stale_running_row_is_retried_once_then_failed(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    rid = await make_request(sessions, job.id)
    async with sessions.begin() as s:
        await s.execute(update(ScoreRequest).values(status="running"))
    scoring = FakeScoring()
    stale = datetime.now(UTC) + timedelta(seconds=STALE_RUNNING_S + 60)
    assert STALE_RUNNING_S == 35 * 60
    assert await score_request_tick(deps_for(sessions, scoring), "w2", now=stale) is True
    assert len(scoring.calls) == 1 and (await row_of(sessions, rid)).status == "done"
    # a second stale running row that was already retried once is failed, not run again
    async with sessions.begin() as s:
        await s.execute(update(ScoreRequest).values(status="running", detail="retried"))
    much_later = stale + timedelta(seconds=STALE_RUNNING_S + 60)
    assert await score_request_tick(deps_for(sessions, scoring), "w2", now=much_later) is True
    row = await row_of(sessions, rid)
    assert len(scoring.calls) == 1 and row.status == "failed" and "retried" in (row.detail or "")


async def test_claims_oldest_pending_first_and_skips_locked(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    first = await make_request(sessions, job.id, force=True)
    second = await make_request(sessions, job.id)
    async with sessions.begin() as locker:
        locked = await locker.scalar(
            select(ScoreRequest.id)
            .where(ScoreRequest.id == first)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert locked == first
        scoring = FakeScoring()
        assert await score_request_tick(deps_for(sessions, scoring), "w1") is True
        assert scoring.calls == [(job.id, False, None)]  # skipped the locked first row
    assert (await row_of(sessions, second)).status == "done"
    assert (await row_of(sessions, first)).status == "pending"
