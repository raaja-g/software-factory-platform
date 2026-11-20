"""M10.4: the score node never blocks delivery and never reaches the human gate."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from tests.fakes.deps import make_test_deps, seed_demo
from tests.graph.test_loop import FIXTURES, ctx, events
from tests.support.db_helpers import enq

from swfactory.engines.fake import FakeScript
from swfactory.graph import build
from swfactory.graph.build import build_job_graph, initial_state
from swfactory.graph.steps import pinned_definition

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _bind(sessions):
    ctx.sessions = sessions


class Raising:
    async def score_job(self, deps, job_id, **kw):
        raise RuntimeError("scoring exploded")


class Sleeping:
    async def score_job(self, deps, job_id, **kw):
        await asyncio.sleep(5)


async def run_with(scoring):
    sessions = ctx.sessions
    await seed_demo(sessions, defaults={"engine_retry_backoff_s": [0, 0]})
    job = await enq(sessions, app="demo")
    deps = make_test_deps(sessions, FakeScript.from_yaml(FIXTURES / "happy.yaml"))
    object.__setattr__(deps, "scoring", scoring)
    row, defn, _ = await pinned_definition(deps, job.id)
    g = build_job_graph(deps, InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": str(job.id)}}
    st = await g.ainvoke(initial_state(row, defn), cfg)
    return job, st


async def test_score_failure_never_reaches_human_gate():
    job, final = await run_with(Raising())
    assert final["outcome"] == "merged" and await events(job.id, "human.waiting") == []


async def test_score_timeout_ends_graph(monkeypatch):
    monkeypatch.setattr(build, "SCORE_RUN_TIMEOUT_S", 1)
    job, final = await run_with(Sleeping())
    assert final["outcome"] == "merged" and await events(job.id, "human.waiting") == []


async def test_score_node_calls_service():
    calls = []

    class Rec:
        async def score_job(self, deps, job_id, **kw):
            calls.append(job_id)

    job, _ = await run_with(Rec())
    assert calls == [job.id]


_ = SimpleNamespace
