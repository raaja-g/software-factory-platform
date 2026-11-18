import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from tests.support.db_helpers import activate, enq

from swfactory.costs.budgets import BudgetExceeded, BudgetService
from swfactory.costs.pricing import STEP_MIN_USD, estimate_cost
from swfactory.db.tables import AgentRun, DefinitionRow
from swfactory.definition.store import DefinitionStore
from swfactory.domain.types import Role, Usage, reviewer_role
from swfactory.models.catalog import ModelInfo


def info(pin: Decimal, pout: Decimal) -> ModelInfo:
    return ModelInfo("m", "p", "u", "mid", pin, pout, None, True, True, True, None, None, None)


def test_estimate_cost():  # 1M in at $0.30/Mtok + 200k out at $2.50/Mtok
    cost = estimate_cost(info(Decimal("0.30"), Decimal("2.50")), Usage(1_000_000, 200_000))
    assert cost == Decimal("0.800000")


def test_estimate_cost_counts_cache_reads_as_input():
    u = Usage(500_000, 0, cache_read_tokens=500_000)
    assert estimate_cost(info(Decimal("1"), Decimal("1")), u) == Decimal("1.000000")


def test_step_minimums():
    assert STEP_MIN_USD[Role.BUILDER] == Decimal("0.50")
    assert STEP_MIN_USD[Role.APPROVER] == Decimal("0.05")
    assert set(STEP_MIN_USD) == set(Role)


# ----------------------------------------------------------------------------- db


async def setup(sessions, *, daily="50", app_cap=None, apps=("a",)):
    """Activate a definition with the given budgets; return (service, store)."""
    sha = await activate(sessions, {a: {} for a in apps})
    store = DefinitionStore(sessions)
    _, defn = await store.active()
    budgets = defn.factory.budgets.model_copy(
        update={
            "daily_usd": Decimal(daily),
            "app_monthly_usd": {"a": Decimal(app_cap)} if app_cap else {},
        }
    )
    fac = defn.factory.model_copy(update={"budgets": budgets})
    new = defn.model_copy(update={"factory": fac})
    async with sessions.begin() as s:
        row = await s.get(DefinitionRow, sha)
        assert row is not None
        row.content = new.model_dump(mode="json", by_alias=True)
    from swfactory.definition import store as store_mod

    store_mod._CACHE.pop(sha, None)
    return BudgetService(sessions, store), store


async def job(sessions, *, budget="10", app="a", kind="normal"):
    j = await enq(sessions, app=app, feature=f"feature {uuid4()}")
    async with sessions.begin() as s:
        await s.execute(
            text("update app.jobs set budget_usd = :b, kind = :k where id = :i"),
            {"b": Decimal(budget), "k": kind, "i": j.id},
        )
    return j.id


async def spend(sessions, job_id, amount, *, when=None):
    async with sessions.begin() as s:
        s.add(
            AgentRun(
                job_id=job_id,
                role="builder",
                round=0,
                attempt=1 + int(uuid4().int % 10_000_000),
                engine="fake",
                cost_usd=Decimal(amount),
                started_at=when or datetime.now(UTC),
            )
        )


pytestmark_db = pytest.mark.db


@pytest.mark.db
async def test_job_budget_blocks_step(sessions):
    budgets, _ = await setup(sessions)
    j = await job(sessions)
    await spend(sessions, j, "9.60")
    with pytest.raises(BudgetExceeded) as e:
        await budgets.check_before_step(j, Role.BUILDER)
    assert (e.value.scope, e.value.remaining, e.value.gate_reason) == (
        "job",
        Decimal("0.40"),
        "budget",
    )
    await budgets.check_before_step(j, Role.APPROVER)


@pytest.mark.db
async def test_daily_budget_spans_jobs(sessions):
    budgets, _ = await setup(sessions)
    a, b, bench = await job(sessions), await job(sessions), await job(sessions, kind="benchmark")
    await spend(sessions, a, "30")
    await spend(sessions, b, "19.95")
    await spend(sessions, bench, "100")  # benchmark spend is excluded
    other = await job(sessions)
    with pytest.raises(BudgetExceeded) as e:
        await budgets.check_before_step(other, Role.SPEC_WRITER)
    assert e.value.scope == "daily"
    assert await budgets.remaining_today() == Decimal("0.05")


@pytest.mark.db
async def test_yesterdays_spend_is_not_today(sessions):
    budgets, _ = await setup(sessions)
    j = await job(sessions)
    await spend(sessions, j, "49", when=datetime.now(UTC) - timedelta(days=2))
    assert await budgets.remaining_today() == Decimal("50")


@pytest.mark.db
async def test_app_monthly_and_run_cap(sessions):
    budgets, _ = await setup(sessions, app_cap="20", daily="30")
    j = await job(sessions, budget="10")
    other = await job(sessions, budget="100")
    await spend(sessions, other, "5")
    await spend(sessions, j, "5")
    # app month: 20 - 10 spent = 10 left; job 5; daily 30 - 10 = 20: the job remainder wins
    assert await budgets.reserve_run(j, run_id=uuid4(), role=Role.BUILDER, round=1) == Decimal("5")
    assert await budgets.remaining_for_app_month("a") == Decimal("5")
    assert await budgets.remaining_for_app_month("no-cap-app") is None
    with pytest.raises(BudgetExceeded) as e:
        await budgets.reserve_run(other, run_id=uuid4(), role=Role.BUILDER, round=1)
        await budgets.reserve_run(other, run_id=uuid4(), role=Role.BUILDER, round=1)
    assert e.value.scope == "app_month"


@pytest.mark.db
async def test_parallel_reviewers_share_remainder(sessions):
    budgets, _ = await setup(sessions)
    j = await job(sessions, budget="10")
    await spend(sessions, j, "9")
    kinds = ["security", "ux", "ui", "code"]
    caps = await asyncio.gather(
        *(
            budgets.reserve_run(j, run_id=uuid4(), role=reviewer_role(k), round=1, fan_out=4)
            for k in kinds
        )
    )
    assert sum(caps) <= Decimal("1") and min(caps) >= STEP_MIN_USD[Role.REVIEWER_CODE]


@pytest.mark.db
async def test_reviewer_retry_does_not_starve_siblings(sessions):
    budgets, _ = await setup(sessions)
    j = await job(sessions, budget="10")
    await spend(sessions, j, "9")
    r1 = uuid4()
    await budgets.reserve_run(j, run_id=r1, role=Role.REVIEWER_CODE, round=1, fan_out=4)
    await budgets.release_run(r1, Decimal("0.02"))
    retry = await budgets.reserve_run(
        j, run_id=uuid4(), role=Role.REVIEWER_CODE, round=1, fan_out=4
    )
    others = [
        await budgets.reserve_run(j, run_id=uuid4(), role=reviewer_role(k), round=1, fan_out=4)
        for k in ("security", "ux", "ui")
    ]
    assert retry <= Decimal("0.25") and min(others) >= STEP_MIN_USD[Role.REVIEWER_CODE]
    assert retry + sum(others) <= Decimal("1")


@pytest.mark.db
async def test_concurrent_jobs_share_daily(sessions):
    budgets, _ = await setup(sessions)
    filler = await job(sessions, budget="100")
    await spend(sessions, filler, "49")
    j1, j2 = await job(sessions), await job(sessions)
    a, b = await asyncio.gather(
        budgets.reserve_run(j1, run_id=uuid4(), role=Role.BUILDER, round=1),
        budgets.reserve_run(j2, run_id=uuid4(), role=Role.BUILDER, round=1),
        return_exceptions=True,
    )
    got = [x for x in (a, b) if isinstance(x, Decimal)]
    assert sum(got) <= Decimal("1")
    lost = [x for x in (a, b) if isinstance(x, BudgetExceeded)]
    assert [x.scope for x in lost] == ["daily"]


@pytest.mark.db
async def test_step_ceiling_leaves_daily_room(sessions):
    budgets, _ = await setup(sessions)
    filler = await job(sessions, budget="100")
    await spend(sessions, filler, "20")
    js = [await job(sessions) for _ in range(4)]
    caps = await asyncio.gather(
        *(budgets.reserve_run(j, run_id=uuid4(), role=Role.BUILDER, round=1) for j in js[:3])
    )
    assert caps == [Decimal("5")] * 3
    await budgets.check_before_step(js[3], Role.APPROVER)


@pytest.mark.db
async def test_daily_refusal_reports_reserved(sessions):
    budgets, _ = await setup(sessions)
    filler = await job(sessions, budget="100")
    await spend(sessions, filler, "45")
    holder, other = await job(sessions), await job(sessions)
    await budgets.reserve_run(holder, run_id=uuid4(), role=Role.BUILDER, round=1)
    with pytest.raises(BudgetExceeded) as e:
        await budgets.check_before_step(other, Role.APPROVER)
    assert (e.value.scope, e.value.remaining, e.value.reserved) == (
        "daily",
        Decimal("0"),
        Decimal("5"),
    )


@pytest.mark.db
async def test_open_reservation_counts_until_released(sessions):
    budgets, _ = await setup(sessions, daily="500")
    j = await job(sessions, budget="10")
    await spend(sessions, j, "4.60")
    r1 = uuid4()
    assert await budgets.reserve_run(j, run_id=r1, role=Role.BUILDER, round=1) == Decimal("5")
    with pytest.raises(BudgetExceeded):
        await budgets.check_before_step(j, Role.BUILDER)
    await budgets.release_run(r1, Decimal("0.70"))
    await spend(sessions, j, "0.70")
    await budgets.release_run(r1, Decimal("9"))  # second release is a no-op
    await budgets.check_before_step(j, Role.BUILDER)
