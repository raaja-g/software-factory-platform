"""M10.13: a benchmark job is held to its job cap and its run's cap, never the daily cap."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from tests.unit.costs.test_budgets import job, setup, spend

from swfactory.costs.budgets import BudgetExceeded
from swfactory.db.tables import BenchResult, BenchRun
from swfactory.domain.types import Role

pytestmark = pytest.mark.db

TABLES = (
    "app.bench_results, app.bench_runs, app.budget_reservations, app.model_decisions, "
    "app.agent_runs, app.job_events, app.jobs"
)


async def _clean(sessions):
    async with sessions.begin() as s:
        await s.execute(text(f"truncate {TABLES} cascade"))


@pytest.fixture
async def env(sessions):
    await _clean(sessions)
    yield sessions
    await _clean(sessions)


async def bench_job(sessions, *, run_budget="10", job_budget="10") -> tuple[UUID, UUID]:
    """A benchmark job that is trial 1 of a new run; returns (job, run)."""
    jid = await job(sessions, budget=job_budget, kind="benchmark")
    async with sessions.begin() as s:
        run = BenchRun(
            suite="s", configurations=[], status="running", budget_usd=Decimal(run_budget)
        )
        s.add(run)
        await s.flush()
        s.add(BenchResult(run_id=run.id, task="t", configuration="c", repetition=1,
                          status="running", job_id=jid, overrides={}))  # fmt: skip
        await s.execute(
            text("update app.jobs set source_ref = :r where id = :i"),
            {"r": f"bench:{run.id}:t:c:1", "i": jid},
        )
        return jid, run.id


async def sibling(sessions, run_id: UUID, n: int) -> UUID:
    """Another trial of the same run."""
    jid = await job(sessions, kind="benchmark")
    async with sessions.begin() as s:
        await s.execute(
            text("update app.jobs set source_ref = :r where id = :i"),
            {"r": f"bench:{run_id}:t{n}:c:1", "i": jid},
        )
    return jid


async def test_bench_run_cap_blocks_step(env):
    budgets, _ = await setup(env)
    jid, run = await bench_job(env, run_budget="3")
    other = await sibling(env, run, 2)
    await spend(env, other, "2.80")  # the run has spent nearly all of its 3.00
    with pytest.raises(BudgetExceeded) as e:
        await budgets.check_before_step(jid, Role.BUILDER)
    assert e.value.scope == "bench_run"


async def test_bench_run_cap_counts_judge_spend(env):
    budgets, _ = await setup(env)
    jid, run = await bench_job(env, run_budget="3")
    await spend(env, jid, "3")
    with pytest.raises(BudgetExceeded) as e:
        await budgets.check_before_step(jid, Role.BUILDER)
    assert e.value.scope in {"job", "bench_run"}


async def test_bench_run_with_room_passes(env):
    budgets, _ = await setup(env)
    jid, _ = await bench_job(env)
    assert await budgets.check_before_step(jid, Role.BUILDER) is None


async def test_bench_job_skips_daily_scope(env):
    budgets, _ = await setup(env, daily="1")
    other = await job(env)  # a normal job spends today's whole daily cap
    await spend(env, other, "50")
    jid, _ = await bench_job(env)
    assert await budgets.check_before_step(jid, Role.BUILDER) is None


async def test_normal_job_still_hits_daily_scope(env):
    budgets, _ = await setup(env, daily="1")
    other = await job(env)
    await spend(env, other, "50")
    fresh = await job(env)
    with pytest.raises(BudgetExceeded) as e:
        await budgets.check_before_step(fresh, Role.BUILDER)
    assert e.value.scope == "daily"


async def test_reserve_run_respects_the_bench_run_cap(env):
    budgets, _ = await setup(env)
    jid, run = await bench_job(env, run_budget="3")
    await spend(env, await sibling(env, run, 2), "2.80")
    with pytest.raises(BudgetExceeded) as e:
        await budgets.reserve_run(jid, run_id=uuid4(), role=Role.BUILDER, round=1)
    assert e.value.scope == "bench_run"
