from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text

from swfactory.bench.runner import EstimateChanged, start_bench_run
from swfactory.db.tables import BenchResult, BenchRun
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import (
    BenchConfigDef,
    BenchSuiteDef,
    BenchTaskDef,
    Definition,
    RoleOverride,
)
from swfactory.domain.types import Role, SelectionMode, TriageMode

pytestmark = pytest.mark.db

SHA = "a" * 40
TASKS = ["001-a", "002-b", "003-c", "004-d"]
GEMINI = BenchConfigDef(
    name="gemini",
    profile="team-gemini",
    roles={Role.BUILDER: RoleOverride(model="gemini-x", mode=SelectionMode.FIXED)},
)
CLAUDE = BenchConfigDef(name="claude", profile="team-claude")


def _defn(base: Definition) -> Definition:
    task = BenchTaskDef(
        title="t",
        feature="f",
        repo="r",
        base_sha=SHA,
        merge_sha=SHA,
        definition_sha=SHA,
        check_command="pytest {file}",
        scorers=("merge-readiness", "scope-creep"),
    )
    suite = BenchSuiteDef(configurations=(GEMINI, CLAUDE), tasks={n: task for n in TASKS})
    return base.model_copy(update={"benchmarks": {"v1-parity": suite}})


def _base_definition(tmp_path: Path) -> Definition:
    init_definition_repo(tmp_path)
    r = load_definition(str(tmp_path))
    assert r.definition is not None
    return r.definition


async def _clean(sessions: Any) -> None:
    async with sessions.begin() as s:
        await s.execute(text("truncate app.bench_results, app.bench_runs cascade"))


@pytest.fixture
async def defn(sessions, tmp_path):
    await _clean(sessions)
    try:
        yield _defn(_base_definition(tmp_path))
    finally:
        await _clean(sessions)


async def test_start_refuses_changed_estimate(sessions, defn):
    async with sessions() as s:
        with pytest.raises(EstimateChanged):
            await start_bench_run(
                s, defn, suite="v1-parity", configs=["gemini"], repetitions=1,
                budget_usd=None, definition_sha="x", accept_estimate_usd=Decimal("1.00"),
                requested_by="admin",
            )  # fmt: skip
    async with sessions() as s:
        assert (await s.scalars(select(BenchRun))).all() == []


async def test_start_inserts_pending_in_suite_order(sessions, defn):
    # no history, no source jobs: 4 trials x (job cap 10 + 2 judges 0.04)
    async with sessions() as s:
        run = await start_bench_run(
            s, defn, suite="v1-parity", configs=["gemini"], repetitions=1,
            budget_usd=None, definition_sha="sha1", accept_estimate_usd=Decimal("40.16"),
            requested_by="admin",
        )  # fmt: skip
    async with sessions() as s:
        rows = (await s.scalars(select(BenchResult).where(BenchResult.run_id == run))).all()
        stored = await s.get(BenchRun, run)
    assert [r.task for r in rows] == TASKS
    assert {r.status for r in rows} == {"pending"}
    assert stored is not None
    assert stored.configurations == [GEMINI.model_dump(mode="json")]
    assert (stored.status, stored.repetitions, stored.estimate_usd, stored.requested_by) == (
        "running", 1, Decimal("40.16"), "admin",
    )  # fmt: skip
    assert stored.budget_usd == Decimal("40") and stored.definition_sha == "sha1"
    assert rows[0].overrides["roles"]["builder"]["model"] == "gemini-x"


async def test_start_repetitions_and_all_configs(sessions, defn):
    async with sessions() as s:
        run = await start_bench_run(
            s, defn, suite="v1-parity", configs=None, repetitions=2,
            budget_usd=Decimal("5"), definition_sha="sha1",
            accept_estimate_usd=Decimal("160.64"), requested_by="admin",
        )  # fmt: skip
    async with sessions() as s:
        rows = (await s.scalars(select(BenchResult).where(BenchResult.run_id == run))).all()
    assert len(rows) == 16 and {r.repetition for r in rows} == {1, 2}


async def test_unknown_suite_and_config(sessions, defn):
    async with sessions() as s:
        with pytest.raises(ValueError, match="no such benchmark suite"):
            await start_bench_run(
                s, defn, suite="nope", configs=None, repetitions=1, budget_usd=None,
                definition_sha="x", accept_estimate_usd=Decimal(0), requested_by="a",
            )  # fmt: skip
        with pytest.raises(ValueError, match="unknown configuration"):
            await start_bench_run(
                s, defn, suite="v1-parity", configs=["zzz"], repetitions=1, budget_usd=None,
                definition_sha="x", accept_estimate_usd=Decimal(0), requested_by="a",
            )  # fmt: skip


async def test_inline_configs_reach_trials(sessions, defn):
    fixed = BenchConfigDef(
        name="fixed", profile="team-gemini", triage_modes={"kind": TriageMode.RULES}
    )
    laya = BenchConfigDef(
        name="laya",
        profile="team-gemini",
        triage_modes={"kind": TriageMode.LAYA},
        laya_heads={"kind": 3},
    )
    async with sessions() as s:
        with pytest.raises(ValueError, match="duplicate configuration name"):
            await start_bench_run(
                s, defn, suite="v1-parity", configs=[fixed, fixed], repetitions=1,
                budget_usd=None, definition_sha="x", accept_estimate_usd=Decimal(0),
                requested_by="a",
            )  # fmt: skip
        with pytest.raises(ValueError, match="unknown profile"):
            await start_bench_run(
                s, defn, suite="v1-parity", repetitions=1, budget_usd=None,
                configs=[BenchConfigDef(name="x", profile="nope")], definition_sha="x",
                accept_estimate_usd=Decimal(0), requested_by="a",
            )  # fmt: skip
        # a suite configuration by name and an inline one in the same run
        run = await start_bench_run(
            s, defn, suite="v1-parity", configs=["claude", laya], repetitions=1,
            budget_usd=None, definition_sha="sha1", accept_estimate_usd=Decimal("80.32"),
            requested_by="admin",
        )  # fmt: skip
    async with sessions() as s:
        rows = (await s.scalars(select(BenchResult).where(BenchResult.run_id == run))).all()
        stored = await s.get(BenchRun, run)
    assert stored is not None
    assert stored.configurations == [CLAUDE.model_dump(mode="json"), laya.model_dump(mode="json")]
    by = {r.configuration: r.overrides for r in rows}
    assert by["laya"]["laya_heads"] == {"kind": 3} and by["laya"]["triage_modes"] == {
        "kind": "laya"
    }
    assert by["claude"]["laya_heads"] == {} and len(rows) == 8
    assert BenchConfigDef.model_validate(stored.configurations[1]) == laya
    assert fixed.laya_heads == {}
