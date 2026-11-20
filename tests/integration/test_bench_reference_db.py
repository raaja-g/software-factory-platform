"""M10.12b: the runner checks a task's hidden checks before its first trial."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.integration.test_bench_runner_db import (
    SUITE_NAME,
    T0,
    TASKS,
    finish,
    jobs,
    make_env,
    statuses,
    task_of,
)

from swfactory.bench.reference import task_check_key
from swfactory.db.tables import BenchResult, BenchTaskCheck, Job

pytestmark = pytest.mark.db


@pytest.fixture
async def runner(sessions):
    async for out in make_env(sessions, valid_checks=False):
        yield out


def key_of(task: str) -> str:
    return task_check_key(SUITE_NAME, task_of(task))


async def set_check(sessions, task: str, status: str, reason: str = "") -> None:
    async with sessions.begin() as s:
        row = await s.get(BenchTaskCheck, key_of(task))
        if row is None:
            s.add(
                BenchTaskCheck(
                    check_key=key_of(task),
                    suite=SUITE_NAME,
                    task=task,
                    status=status,
                    detail={"reason": reason},
                )
            )
        else:
            row.status, row.detail = status, {"reason": reason}


async def check_row(sessions, task: str) -> BenchTaskCheck:
    async with sessions() as s:
        row = await s.get(BenchTaskCheck, key_of(task))
    assert row is not None
    return row


async def test_tick_checks_before_first_trial(runner):
    sessions, rn, run, _ = runner
    await rn.tick(T0)
    refs = [j.source_ref for j in await jobs(sessions, "benchmark")]
    assert refs == [f"bench-check:{key_of(TASKS[0])}"]
    assert await statuses(sessions, run) == {"pending": 12}
    row = await check_row(sessions, TASKS[0])
    assert row.status == "pending" and row.job_id is not None
    (job,) = await jobs(sessions, "benchmark")
    assert job.branch == f"bench/check-{key_of(TASKS[0])[:8]}/{TASKS[0]}"
    assert job.title == f"check {TASKS[0]}" and job.profile_name == "team-gemini"
    # the job is not over: the next tick starts nothing and does not enqueue a second one
    assert await rn.tick(T0) == []
    assert len(await jobs(sessions, "benchmark")) == 1


async def test_valid_check_lets_trial_start(runner):
    sessions, rn, run, _ = runner
    await rn.tick(T0)
    await set_check(sessions, TASKS[0], "valid")
    await finish(sessions, (await jobs(sessions, "benchmark"))[0].id)
    await rn.tick(T0)
    assert [(j.source_ref or "").split(":")[0] for j in await jobs(sessions, "benchmark")] == [
        "bench-check",
        "bench",
    ]
    assert (await statuses(sessions, run)) == {"pending": 11, "running": 1}


async def drain(sessions, rn, run) -> None:
    for _ in range(30):
        await rn.tick(T0)
        async with sessions.begin() as s:
            for j in (await s.scalars(select(Job).where(Job.stage != "closed"))).all():
                if j.source_ref and j.source_ref.startswith("bench-check:"):
                    key = j.source_ref.split(":", 1)[1]
                    row = await s.get(BenchTaskCheck, key)
                    if row is not None and row.status in ("pending", "running"):
                        row.status = "valid"
                j.stage = "closed"
    await rn.tick(T0)


async def test_invalid_task_skipped(runner):
    sessions, rn, run, _ = runner
    for t in (TASKS[0], TASKS[1], TASKS[3]):
        await set_check(sessions, t, "valid")
    await set_check(sessions, TASKS[2], "invalid", "tests/test_web.py passes at the base commit")
    await drain(sessions, rn, run)
    async with sessions() as s:
        rows = (await s.scalars(select(BenchResult).where(BenchResult.task == "003-c"))).all()
    assert {r.status for r in rows} == {"skipped_invalid"}
    assert (await statuses(sessions, run))["done"] == 9


async def test_valid_check_reused_by_later_runs(runner):
    sessions, rn, run, _ = runner
    for t in TASKS:
        await set_check(sessions, t, "valid")
    await rn.tick(T0)
    assert ((await jobs(sessions, "benchmark"))[0].source_ref or "").startswith("bench:")


async def test_check_job_that_ended_without_verdict_is_invalid(runner):
    sessions, rn, run, _ = runner
    await rn.tick(T0)
    await finish(sessions, (await jobs(sessions, "benchmark"))[0].id)
    await rn.tick(T0)
    row = await check_row(sessions, TASKS[0])
    assert row.status == "invalid" and "without a verdict" in row.detail["reason"]
    async with sessions() as s:
        skipped = (await s.scalars(select(BenchResult).where(BenchResult.task == TASKS[0]))).all()
    assert {r.status for r in skipped} == {"skipped_invalid"}
