"""M10.13: a benchmark job runs under its configuration's role and triage overrides."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from tests.fakes.deps import make_test_deps, seed_demo
from tests.support.db_helpers import enq

from swfactory.db.repo import insert_model_decision
from swfactory.db.tables import BenchResult, BenchRun, Job, ModelDecision
from swfactory.domain.types import JobKind, ModelRef, Role, SelectionMode, ToolPolicy, TriageMode
from swfactory.engines.fake import FakeScript
from swfactory.graph.build import initial_state
from swfactory.graph.nodes import triage as triage_node
from swfactory.graph.steps import _runner, pinned_definition, run_agent_step
from swfactory.models.selection import ModelChoice
from swfactory.sandbox.paths import REPO

pytestmark = pytest.mark.db

LAYA = {"roles": {"builder": {"mode": "laya"}}, "triage_modes": {"kind": "laya"}}
TABLES = (
    "app.bench_results, app.bench_runs, app.model_decisions, app.agent_runs, "
    "app.job_events, app.jobs"
)


class ProfileSelector:
    """Records a decision row with the mode the resolved profile gives the role."""

    def __init__(self, sessions):
        self.sessions = sessions

    async def choose(self, profile, ctx, *, exclude=()):
        rm = profile.for_role(ctx.role)
        async with self.sessions() as s, s.begin():
            did = await insert_model_decision(
                s, job_id=ctx.job_id, role=ctx.role.value, round=ctx.round, mode=rm.mode.value,
                options=[{"alias": "fake"}], chosen="fake", reason="test", latency_ms=0,
            )  # fmt: skip
        return ModelChoice(
            engine="fake", model=ModelRef("fake", "fake", "fake"), mode=rm.mode, tier=None,
            decision_id=did, reason="test",
        )  # fmt: skip


class RecordingTriage:
    def __init__(self, inner):
        self.inner = inner
        self.calls: list[SimpleNamespace] = []

    async def triage(self, job, app, *, modes=None, pins=None):
        self.calls.append(SimpleNamespace(modes=modes))
        return await self.inner.triage(job, app, modes=modes, pins=pins)


async def _clean(sessions):
    async with sessions.begin() as s:
        await s.execute(text(f"truncate {TABLES} cascade"))


@pytest.fixture
async def env(sessions):
    await _clean(sessions)
    yield sessions
    await _clean(sessions)


async def setup(sessions, kind, overrides):
    await seed_demo(sessions, defaults={"engine_retry_backoff_s": [0, 0]})
    job = await enq(sessions, app="demo")
    if kind == "benchmark":
        async with sessions.begin() as s:
            (await s.get_one(Job, job.id)).kind = JobKind.BENCHMARK.value
            run = BenchRun(suite="s", configurations=[], status="running", budget_usd=Decimal(10))
            s.add(run)
            await s.flush()
            s.add(BenchResult(run_id=run.id, task="t", configuration="c", repetition=1,
                              status="running", job_id=job.id, overrides=overrides))  # fmt: skip
    script = FakeScript.from_dict(
        {"turns": [{"role": "builder", "round": 1, "attempt": 1, "output": "## Round 1\nok"}]}
    )
    deps = make_test_deps(sessions, script, selector=ProfileSelector(sessions))
    tri = RecordingTriage(deps.triage)
    deps = replace(deps, triage=tri)  # pyright: ignore[reportArgumentType]
    return job, deps, tri


async def run_builder(job, deps):
    _, defn, app = await pinned_definition(deps, job.id)
    await deps.sandboxes.ensure(job.id, _runner(defn, app))
    await run_agent_step(
        deps, job_id=job.id, role=Role.BUILDER, round=1, inputs=[],
        output_name="build-r1.md", workdir=REPO, tool_policy=ToolPolicy.READ_ONLY,
    )  # fmt: skip


async def modes_of(sessions, job_id):
    async with sessions() as s:
        rows = await s.scalars(
            select(ModelDecision).where(
                ModelDecision.job_id == job_id,
                ModelDecision.role == "builder",
                ModelDecision.purpose == "model",
            )
        )
        return {r.mode for r in rows}


async def run_triage(job, deps):
    row, defn, _ = await pinned_definition(deps, job.id)
    await triage_node.make(deps)(initial_state(row, defn))


async def test_benchmark_job_under_laya_overrides(env):
    job, deps, tri = await setup(env, "benchmark", LAYA)
    await run_builder(job, deps)
    await run_triage(job, deps)
    assert await modes_of(env, job.id) == {SelectionMode.LAYA.value}
    assert tri.calls[-1].modes == {"kind": TriageMode.LAYA}


async def test_normal_job_ignores_overrides(env):
    job, deps, tri = await setup(env, "normal", LAYA)
    await run_builder(job, deps)
    await run_triage(job, deps)
    assert await modes_of(env, job.id) == {SelectionMode.FIXED.value}
    assert tri.calls[-1].modes is None


async def test_benchmark_job_with_empty_overrides_changes_nothing(env):
    job, deps, tri = await setup(env, "benchmark", {"roles": {}, "triage_modes": {}})
    await run_builder(job, deps)
    await run_triage(job, deps)
    assert await modes_of(env, job.id) == {SelectionMode.FIXED.value}
    assert tri.calls[-1].modes is None
