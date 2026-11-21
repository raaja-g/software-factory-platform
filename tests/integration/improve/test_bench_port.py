"""RunnerBenchPort against the real benchmark runner and a real database."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.pg import truncate_app_tables

from swfactory.db.tables import BenchRun, Improvement
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.store import DefinitionStore
from swfactory.improve.graph import CycleCapReached, RunnerBenchPort

pytestmark = pytest.mark.db

BASE_SHA = "b" * 40


@pytest.fixture
async def world(sessions):
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        result = load_definition(d)
    assert result.definition is not None
    store = DefinitionStore(sessions)
    assert await store.save(result)
    suite = result.definition.benchmarks["v1-parity"]
    assert [c.name for c in suite.configurations] == ["gemini", "claude", "economy"]
    assert suite.budget_usd == Decimal("40")
    yield type("W", (), {"sessions": sessions, "store": store, "sha": result.sha})
    async with sessions() as s:
        await truncate_app_tables(await s.connection())


async def test_port_benchmarks_first_configuration_once(world):
    cycle = uuid4()
    port = RunnerBenchPort(world.sessions, world.store)
    run = await port.start(
        "v1-parity", definition_sha=world.sha, cycle_id=cycle, max_cycle_usd=Decimal("120")
    )
    async with world.sessions() as s:
        row = await s.get(BenchRun, run)
    assert row is not None
    assert ([c["name"] for c in row.configurations], row.repetitions, row.budget_usd) == (
        ["gemini"],
        1,
        Decimal("40"),
    )
    assert row.requested_by == "improver"
    assert await port.status(run) == "running"
    assert await port.find_run("v1-parity", world.sha) == run


async def test_port_refuses_past_cycle_cap(world):
    cycle = uuid4()
    async with world.sessions.begin() as s:
        runs = [
            BenchRun(suite="v1-parity", definition_sha=BASE_SHA, budget_usd=Decimal("40"))
            for _ in range(2)
        ]
        s.add_all(runs)
        await s.flush()
        s.add(
            Improvement(
                trigger="manual",
                role_group="builder",
                base_sha=BASE_SHA,
                suite="v1-parity",
                cycle_id=cycle,
                evidence={"started_bench_runs": [str(r.id) for r in runs]},
            )
        )
    port = RunnerBenchPort(world.sessions, world.store)
    with pytest.raises(CycleCapReached) as exc:
        await port.start(
            "v1-parity", definition_sha=world.sha, cycle_id=cycle, max_cycle_usd=Decimal("100")
        )
    assert (exc.value.committed, exc.value.cap, exc.value.max_cycle_usd) == (
        Decimal("80"),
        Decimal("40"),
        Decimal("100"),
    )
    async with world.sessions() as s:
        runs_now = list(await s.scalars(select(BenchRun)))
    assert len(runs_now) == 2  # nothing new was started


async def test_other_cycles_do_not_count(world):
    port = RunnerBenchPort(world.sessions, world.store)
    run = await port.start(
        "v1-parity", definition_sha=world.sha, cycle_id=uuid4(), max_cycle_usd=Decimal("40")
    )
    assert run is not None


async def test_find_run_ignores_stopped_runs(world):
    async with world.sessions.begin() as s:
        for status in ("cancelled", "stopped_budget"):
            s.add(BenchRun(suite="v1-parity", definition_sha=BASE_SHA, status=status))
    port = RunnerBenchPort(world.sessions, world.store)
    assert await port.find_run("v1-parity", BASE_SHA) is None
    async with world.sessions() as s:
        stopped = await s.scalar(select(BenchRun.id).where(BenchRun.status == "stopped_budget"))
    assert stopped is not None
    assert await port.status(stopped) == "failed"
