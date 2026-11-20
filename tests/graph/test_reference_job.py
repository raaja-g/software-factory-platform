"""M10.12b: a reference job runs no agent, checks the files at the merge and the base."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select, text
from tests.fakes.deps import make_test_deps, seed_demo
from tests.support.db_helpers import enq

from swfactory.bench.reference import REFERENCE_LOG, reference_source_ref, task_check_key
from swfactory.bench.runner import HIDDEN_CHECKS_BOX
from swfactory.db.tables import AgentRun, BenchResult, BenchTaskCheck, Job
from swfactory.definition.models import BenchConfigDef, BenchSuiteDef, BenchTaskDef
from swfactory.domain.types import JobKind
from swfactory.engines.fake import FakeScript
from swfactory.graph.build import build_job_graph, initial_state
from swfactory.graph.steps import pinned_definition
from swfactory.sandbox.base import ExecResult

pytestmark = pytest.mark.db

SHA = "a" * 40
TASK = BenchTaskDef(
    title="todo web",
    feature="Add a todo web page",
    repo="local:demo",
    base_sha=SHA,
    merge_sha="b" * 40,
    definition_sha=SHA,
    check_command="python3 -m unittest -q {file}",
    check_files={"tests/test_web.py": "x = 1"},
)
KEY = task_check_key("suite", TASK)


async def _clean(sessions):
    async with sessions.begin() as s:
        await s.execute(
            text(
                "truncate app.bench_task_checks, app.bench_results, app.bench_runs, "
                "app.scores, app.agent_runs, app.job_events, app.jobs cascade"
            )
        )


@pytest.fixture
async def env(sessions):
    await _clean(sessions)
    yield sessions
    await _clean(sessions)


async def run(sessions, *, iso_exit: dict, iso_exit_at_base: dict):
    suite = BenchSuiteDef(
        configurations=(BenchConfigDef(name="c1", profile="team-a"),), tasks={"001-todo": TASK}
    )
    await seed_demo(
        sessions, defaults={"engine_retry_backoff_s": [0, 0]}, benchmarks={"suite": suite}
    )
    job = await enq(sessions, app="demo")
    async with sessions.begin() as s:
        row = await s.get_one(Job, job.id)
        row.kind = JobKind.BENCHMARK.value
        row.source_ref = reference_source_ref(KEY)
        row.title = "check 001-todo"
        row.branch = f"bench/check-{KEY[:8]}/001-todo"
        s.add(BenchTaskCheck(check_key=KEY, suite="suite", task="001-todo", job_id=job.id))
    deps = make_test_deps(sessions, FakeScript.from_dict({"turns": []}))
    sb = deps.sandboxes
    orig = sb.verify_box

    @asynccontextmanager
    async def verify_box(job_id, runner, *, name, head):
        async with orig(job_id, runner, name=name, head=head) as box:
            files = sorted(TASK.check_files)
            box.exec_results = [  # pyright: ignore[reportAttributeAccessIssue]
                ExecResult(iso_exit.get(p, 0), "", "", False, 0.1) for p in files
            ] + [ExecResult(iso_exit_at_base.get(p, 1), "", "", False, 0.1) for p in files]
            yield box

    sb.verify_box = verify_box  # type: ignore[method-assign]
    row, defn, _ = await pinned_definition(deps, job.id)
    g = build_job_graph(deps, InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": str(job.id)}}
    visited: list[str] = []
    async for chunk in g.astream(initial_state(row, defn), cfg, stream_mode="updates"):
        visited.extend(k for k in chunk if not k.startswith("__"))
    return SimpleNamespace(deps=deps, job=job.id, visited=visited)


async def check_row(sessions):
    async with sessions() as s:
        row = await s.get(BenchTaskCheck, KEY)
        assert row is not None
        return row


async def test_reference_job_runs_no_agent(env):
    r = await run(env, iso_exit={"tests/test_web.py": 0}, iso_exit_at_base={"tests/test_web.py": 1})
    assert r.visited == ["prepare", "finalize"]
    row = await check_row(env)
    assert row.status == "valid" and row.detail["reason"] == ""
    assert row.detail["at_merge"] == {"tests/test_web.py": 0}
    assert row.detail["at_base"] == {"tests/test_web.py": 1}
    assert r.deps.sandboxes.verify_calls[-1][0] == HIDDEN_CHECKS_BOX
    async with env() as s:
        assert (await s.get_one(Job, r.job, populate_existing=True)).stage == "closed"
        assert await s.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert await s.scalar(select(func.count()).select_from(BenchResult)) == 0
    assert REFERENCE_LOG in {m.path for m in await r.deps.artifacts.list(r.job)}
    iso = r.deps.sandboxes.isolated_boxes[HIDDEN_CHECKS_BOX]
    base_checkout = [c for c in iso.exec_calls if "checkout" in c and SHA in c]
    assert base_checkout and "--detach" in base_checkout[0] and "--force" in base_checkout[0]


async def test_check_passing_at_base_is_invalid(env):
    r = await run(env, iso_exit={"tests/test_web.py": 0}, iso_exit_at_base={"tests/test_web.py": 0})
    row = await check_row(env)
    assert row.status == "invalid"
    assert row.detail["reason"] == "tests/test_web.py passes at the base commit"
    assert r.visited == ["prepare", "finalize"]
