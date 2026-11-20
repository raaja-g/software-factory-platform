from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from swfactory.bench.reference import task_check_key
from swfactory.bench.runner import BenchRunner, bench_branch
from swfactory.db.tables import AgentRun, BenchResult, BenchRun, BenchTaskCheck, Job
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import AppDef, BenchConfigDef, BenchSuiteDef, BenchTaskDef
from swfactory.definition.store import DefinitionStore
from swfactory.domain.types import JobKind, Stage
from swfactory.queue import claim_next_job, enqueue_job

pytestmark = pytest.mark.db

SHA = "a" * 40
TASKS = ["001-bootstrap-python", "002-b", "003-c", "004-d"]
SUITE_NAME = "v1-parity"
CONFIGS = ("gemini", "claude", "codex")
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
ENQ: dict[str, Any] = {
    "app_slug": "v1-parity",
    "feature": "f",
    "title": None,
    "source": "api",
    "source_ref": None,
    "requested_by": "admin",
}
TASK = BenchTaskDef(
    title="t",
    feature="f",
    repo="local:v1-parity",
    base_sha=SHA,
    merge_sha=SHA,
    definition_sha=SHA,
    check_command="pytest {file}",
)
TABLES = "app.bench_task_checks, app.bench_results, app.bench_runs"


def task_of(name: str) -> BenchTaskDef:
    """Each task has its own check file, so each has its own check key."""
    return TASK.model_copy(update={"check_files": {"tests/test_web.py": f"# {name}"}})


async def _clean(sessions: Any) -> None:
    async with sessions.begin() as s:
        await s.execute(
            text(f"truncate {TABLES}, app.agent_runs, app.jobs, app.job_events cascade")
        )


async def make_env(sessions, *, valid_checks: bool):
    await _clean(sessions)
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    base = next(iter(r.definition.factory.apps.values()))
    app = AppDef.model_validate({**base.model_dump(mode="json"), "concurrency": 1})
    fac = r.definition.factory.model_copy(update={"apps": {"v1-parity": app}})
    suite = BenchSuiteDef(
        configurations=tuple(BenchConfigDef(name=c, profile=f"team-{c}") for c in CONFIGS),
        tasks={n: task_of(n) for n in TASKS},
    )
    defn = r.definition.model_copy(update={"factory": fac, "benchmarks": {"v1-parity": suite}})
    sha = f"sha-bench-{datetime.now(UTC).timestamp()}"
    store = DefinitionStore(sessions)
    assert await store.save(replace(r, sha=sha, definition=defn))
    async with sessions.begin() as s:
        run = BenchRun(
            suite="v1-parity",
            configurations=[c.model_dump(mode="json") for c in suite.configurations],
            definition_sha=sha,
            status="running",
            budget_usd=Decimal("40"),
            max_parallel=1,
            requested_by="admin",
        )
        s.add(run)
        await s.flush()
        for t in TASKS:
            for c in CONFIGS:
                s.add(BenchResult(run_id=run.id, task=t, configuration=c, status="pending"))
        if valid_checks:
            for t in TASKS:
                s.add(
                    BenchTaskCheck(
                        check_key=task_check_key(SUITE_NAME, suite.tasks[t]),
                        suite=SUITE_NAME,
                        task=t,
                        status="valid",
                    )
                )
        run_id, def_sha = run.id, sha
    yield sessions, BenchRunner(sessions, store), run_id, def_sha
    await _clean(sessions)


@pytest.fixture
async def env(sessions):
    async for out in make_env(sessions, valid_checks=True):
        yield out


async def jobs(sessions: Any, kind: str | None = None) -> list[Job]:
    async with sessions() as s:
        q = select(Job)
        if kind:
            q = q.where(Job.kind == kind)
        return list((await s.scalars(q.order_by(Job.created_at))).all())


async def statuses(sessions: Any, run: UUID) -> dict[str, int]:
    async with sessions() as s:
        rows = await s.execute(
            select(BenchResult.status, func.count())
            .where(BenchResult.run_id == run)
            .group_by(BenchResult.status)
        )
        return {k: v for k, v in rows}


async def run_status(sessions: Any, run: UUID) -> str:
    async with sessions() as s:
        return (await s.get(BenchRun, run)).status  # type: ignore[union-attr]


async def finish(sessions: Any, job_id: UUID) -> None:
    async with sessions.begin() as s:
        job = await s.get(Job, job_id)
        assert job is not None
        job.stage = Stage.CLOSED.value


async def set_spent(sessions: Any, run: UUID, usd: str) -> None:
    async with sessions.begin() as s:
        job = await enqueue_job(
            s, **{**ENQ, "source_ref": f"bench:{run}:spent:x:1"}, kind=JobKind.BENCHMARK
        )
        job.stage = Stage.CLOSED.value
        s.add(AgentRun(job_id=job.id, role="builder", engine="fake", cost_usd=Decimal(usd)))


async def test_tick_one_at_a_time_and_hard_cap(env):
    sessions, runner, run, def_sha = env
    assert len(await runner.tick(T0)) == 1 and len(await runner.tick(T0)) == 0
    (job,) = await jobs(sessions, "benchmark")
    assert (job.kind, job.branch, job.base_sha, job.definition_sha) == (
        "benchmark",
        bench_branch(run, "001-bootstrap-python", "gemini"),
        SHA,
        def_sha,
    )
    await set_spent(sessions, run, "39.95")
    await finish(sessions, job.id)
    await runner.tick(T1)
    assert await statuses(sessions, run) == {"done": 1, "skipped_budget": 11}
    assert await run_status(sessions, run) == "stopped_budget"


async def test_trial_needs_a_whole_job_cap(env):
    sessions, runner, run, _ = env
    await set_spent(sessions, run, "30.01")
    await runner.tick(T0)
    assert await statuses(sessions, run) == {"skipped_budget": 12}
    assert await run_status(sessions, run) == "stopped_budget"
    assert (
        len([j for j in await jobs(sessions, "benchmark") if "spent" not in (j.source_ref or "")])
        == 0
    )


async def test_trial_gets_the_whole_job_cap(env):
    sessions, runner, _, _ = env
    await runner.tick(T0)
    (job,) = await jobs(sessions, "benchmark")
    assert job.budget_usd == Decimal("10")


async def test_tick_adopts_orphan_job(env):
    sessions, runner, run, _ = env
    async with sessions.begin() as s:
        orphan = await enqueue_job(
            s, **{**ENQ, "source_ref": f"bench:{run}:001-bootstrap-python:gemini:1"},
            kind=JobKind.BENCHMARK,
        )  # fmt: skip
        orphan_id = orphan.id
    await runner.tick(T0)
    async with sessions() as s:
        res = await s.scalar(
            select(BenchResult).where(
                BenchResult.run_id == run, BenchResult.task == "001-bootstrap-python",
                BenchResult.configuration == "gemini",
            )
        )  # fmt: skip
    assert res is not None and res.job_id == orphan_id and res.status == "running"
    assert len(await jobs(sessions, "benchmark")) == 1


async def test_running_benchmark_does_not_block_normal_job(env):
    sessions, runner, _, _ = env
    await runner.tick(T0)
    async with sessions() as s:
        bench = await claim_next_job(s, worker_id="w1", lease_s=90)
    async with sessions.begin() as s:
        normal = await enqueue_job(s, **ENQ)
    async with sessions() as s:
        claimed = await claim_next_job(s, worker_id="w2", lease_s=90)
    assert bench is not None and claimed is not None
    assert (bench.kind, claimed.id, claimed.kind) == ("benchmark", normal.id, "normal")


async def test_max_parallel_1_never_two_benchmarks_running(env):
    sessions, runner, run, _ = env
    peak = 0
    for i in range(12):
        await runner.tick(T0 + timedelta(seconds=30 * i))
        live = [j for j in await jobs(sessions, "benchmark") if j.stage != Stage.CLOSED.value]
        peak = max(peak, (await statuses(sessions, run)).get("running", 0), len(live))
        async with sessions() as s:
            job = await claim_next_job(s, worker_id=f"w{i}", lease_s=90)
        assert job is not None
        await finish(sessions, job.id)
    await runner.tick(T1)
    assert peak == 1 and await statuses(sessions, run) == {"done": 12}
    assert await run_status(sessions, run) == "done"
