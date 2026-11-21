"""The labelling queue: finished jobs whose judge has answered and that you have not labelled."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import update
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.db.repo import label_queue, put_judge_label, replace_scores
from swfactory.db.tables import AgentRun, Job
from swfactory.scoring.objective import ScoreRow

pytestmark = pytest.mark.db

SC, RS, OLD = "merge-readiness", "rubric-1", "rubric-0"


def verdict(passed: bool | None = True, *, rubric: str = RS, scorer: str = SC) -> ScoreRow:
    return ScoreRow(
        scorer, "A" if passed else "C", Decimal(1 if passed else 0), passed, "r",
        judge_model="qwen-27b@groq", rubric_sha=rubric, judge_sha="j1",
    )  # fmt: skip


async def make(sessions, *, finished=True, kind="normal", built=True, judged=True, app="demo"):
    """A job; `judged` gives it a valid judge row, `built` an ok builder run."""
    j = await enq(sessions, app=app)
    async with sessions.begin() as s:
        await s.execute(
            update(Job)
            .where(Job.id == j.id)
            .values(kind=kind, stage="merged", finished_at=Job.created_at if finished else None)
        )
        if built:
            s.add(
                AgentRun(job_id=j.id, role="builder", round=1, attempt=1, engine="e", status="ok")
            )
        if judged:
            await replace_scores(s, j.id, [verdict()], scorers=[SC])
    return j


async def queue(sessions, **kw):
    async with sessions() as s:
        return await label_queue(s, SC, RS, roles=("builder",), **kw)


async def test_lists_judged_unlabelled_jobs_oldest_first(sessions):
    await seed_demo(sessions)
    a, b = await make(sessions), await make(sessions)
    q = await queue(sessions)
    assert [x.ref for x in q.jobs] == [f"demo:{a.seq:03d}", f"demo:{b.seq:03d}"]
    assert (q.total, q.awaiting_judge) == (2, 0)
    assert q.jobs[0].title and q.jobs[0].stage == "merged" and q.jobs[0].app == "demo"


async def test_a_labelled_job_leaves_the_queue_but_an_old_rubric_label_does_not(sessions):
    await seed_demo(sessions)
    done, stale, open_ = [await make(sessions) for _ in range(3)]
    async with sessions.begin() as s:
        await put_judge_label(s, done.id, SC, "A", RS, "me@x.io")
        await put_judge_label(s, stale.id, SC, "A", OLD, "me@x.io")  # an earlier rubric
    refs = [x.ref for x in (await queue(sessions)).jobs]
    assert refs == [f"demo:{stale.seq:03d}", f"demo:{open_.seq:03d}"]


async def test_jobs_without_a_valid_judge_verdict_are_counted_not_listed(sessions):
    await seed_demo(sessions)
    await make(sessions, judged=False)  # never judged
    bad = await make(sessions, judged=False)
    old = await make(sessions, judged=False)
    other = await make(sessions, judged=False)
    async with sessions.begin() as s:
        await replace_scores(s, bad.id, [verdict(None)], scorers=[SC])  # invalid answer
        await replace_scores(s, old.id, [verdict(rubric=OLD)], scorers=[SC])  # other rubric
        await replace_scores(s, other.id, [verdict(scorer="scope-creep")], scorers=["scope-creep"])
    q = await queue(sessions)
    assert (q.jobs, q.total, q.awaiting_judge) == ([], 0, 4)


async def test_unfinished_benchmark_and_unbuilt_jobs_are_left_out(sessions):
    await seed_demo(sessions)
    await make(sessions, finished=False)
    await make(sessions, kind="benchmark")
    await make(sessions, built=False)  # the scorer applies to builder runs only
    q = await queue(sessions)
    assert (q.jobs, q.total, q.awaiting_judge) == ([], 0, 0)


async def test_limit_caps_the_list_not_the_total(sessions):
    await seed_demo(sessions)
    for _ in range(3):
        await make(sessions)
    q = await queue(sessions, limit=2)
    assert (len(q.jobs), q.total) == (2, 3)


async def test_only_the_visible_apps_are_listed(sessions):
    await seed_demo(sessions)
    mine = await make(sessions, app="demo")
    q = await queue(sessions, app_slugs=frozenset({"demo"}))
    assert [x.ref for x in q.jobs] == [f"demo:{mine.seq:03d}"]
    assert (await queue(sessions, app_slugs=frozenset({"nope"}))).total == 0
