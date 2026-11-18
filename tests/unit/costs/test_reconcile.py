"""Spend reconciliation and the reservation sweep (M5.9)."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from tests.support.gateway_env import make_env

from swfactory.costs.reconcile import reconcile_run_costs, reconcile_tick
from swfactory.db.tables import AgentRun, BudgetReservation, Job

pytestmark = pytest.mark.db

COUNTER = [0]
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
async def env(sessions, monkeypatch):
    return await make_env(sessions, monkeypatch)


async def add_run(env, *, status, ended_ago=None, cost=None, reserved_ago=None):
    COUNTER[0] += 1
    n = COUNTER
    rid = uuid4()
    end = NOW - timedelta(seconds=ended_ago) if ended_ago is not None else None
    async with env.sessions.begin() as s:
        s.add(
            AgentRun(
                id=rid,
                job_id=env.job,
                role="builder",
                round=100 + n[0],
                engine="fake",
                status=status,
                started_at=(end or NOW) - timedelta(seconds=30),
                finished_at=end if status != "running" else None,
                cost_usd=cost,
                cost_source="gateway" if cost is not None else None,
            )
        )
        await s.flush()
        if reserved_ago is not None:
            s.add(
                BudgetReservation(
                    run_id=rid,
                    job_id=env.job,
                    role="builder",
                    round=100 + n[0],
                    day=NOW.date(),
                    month=NOW.date().replace(day=1),
                    amount=Decimal(5),
                    created_at=NOW - timedelta(seconds=reserved_ago),
                )
            )
    return rid


async def run(env, rid):
    async with env.sessions() as s:
        return await s.get(AgentRun, rid)


async def resv(env, rid):
    async with env.sessions() as s:
        return await s.get(BudgetReservation, rid)


async def end_job(env, stage, ago):
    async with env.sessions.begin() as s:
        await s.execute(
            text(
                "update app.jobs set stage = :st, finished_at = :f, updated_at = :f where id = :i"
            ),
            {"st": stage, "f": NOW - timedelta(seconds=ago), "i": env.job},
        )


async def test_fills_missing_cost_after_the_settle_wait(env):
    r1 = await add_run(env, status="failed", ended_ago=10)
    r2 = await add_run(env, status="failed", ended_ago=3)
    env.fake.run_spends = {r1: Decimal("0.4"), r2: Decimal("0.1")}
    r = await reconcile_run_costs(env.deps, now=NOW)
    a, b = await run(env, r1), await run(env, r2)
    assert (a.cost_usd, a.cost_source, b.cost_usd, r.costs_filled) == (
        Decimal("0.4"),
        "gateway",
        None,
        1,
    )
    async with env.sessions() as s:
        job = await s.get(Job, env.job)
        n = await s.scalar(
            text("select count(*) from app.job_events where job_id = :i and kind = 'cost.updated'"),
            {"i": env.job},
        )
    assert job is not None and job.cost_usd == Decimal("0.4") and n == 1


async def test_leaves_running_runs_alone(env):
    await add_run(env, status="running", ended_ago=3600)
    await reconcile_run_costs(env.deps, now=NOW)
    assert env.fake.run_spend_calls == []


async def test_releases_orphan_reservation_with_the_run_cost(env):
    r5 = await add_run(env, status="ok", ended_ago=3000, cost=Decimal("2.5"), reserved_ago=660)
    r = await reconcile_run_costs(env.deps, now=NOW)
    res = await resv(env, r5)
    assert res is not None
    assert (res.released_at is not None, res.actual_usd, r.reservations_released) == (
        True,
        Decimal("2.5"),
        1,
    )


async def test_keeps_young_and_running_reservations(env):
    r6 = await add_run(env, status="ok", ended_ago=60, cost=Decimal(1), reserved_ago=300)
    r7 = await add_run(env, status="running", reserved_ago=1800)
    await reconcile_run_costs(env.deps, now=NOW)
    a, b = await resv(env, r6), await resv(env, r7)
    assert a is not None and b is not None
    assert (a.released_at, b.released_at) == (None, None)


async def test_unreadable_spend_keeps_reservation_open(env):
    r8 = await add_run(env, status="failed", ended_ago=1500, reserved_ago=1200)
    env.fake.run_spends = {r8: None}
    await reconcile_run_costs(env.deps, now=NOW)
    run8, res = await run(env, r8), await resv(env, r8)
    assert run8 is not None and res is not None
    assert (run8.cost_usd, res.released_at) == (None, None)


async def test_charges_running_row_of_an_ended_job(env):
    r9 = await add_run(env, status="running", reserved_ago=1200)
    await end_job(env, "cancelled", 1200)
    env.fake.run_spends = {r9: Decimal("0.3")}
    await reconcile_run_costs(env.deps, now=NOW)
    row, res = await run(env, r9), await resv(env, r9)
    assert row is not None and res is not None
    assert (row.status, row.cost_usd, res.actual_usd) == ("failed", Decimal("0.3"), Decimal("0.3"))
    assert f"run-{r9}" in env.fake.revoked_aliases


async def test_one_worker_reconciles_at_a_time(env):
    r = await add_run(env, status="failed", ended_ago=30)
    env.fake.run_spends = {r: Decimal("0.2")}
    real = env.fake.run_spend

    async def slow(run_id: UUID):
        await asyncio.sleep(0.5)
        return await real(run_id)

    env.fake.run_spend = slow
    deps_b = env.deps  # same database; its own transaction per tick
    a, b = await asyncio.gather(reconcile_tick(env.deps, now=NOW), reconcile_tick(deps_b, now=NOW))
    assert [a, b].count(None) == 1
