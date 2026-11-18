import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import claim as claim_helper
from tests.support.db_helpers import enq

import swfactory.worker as worker_mod
from swfactory.db.repo import insert_command
from swfactory.db.tables import AgentRun, Command, Job, JobEvent
from swfactory.worker import run_worker

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parents[1] / "fixtures" / "fake"
FAST = {"engine_retry_backoff_s": [0, 0]}


class Ctx:
    sessions: Any = None
    settings: Any = None
    deps: Any = None
    state: Any = None


ctx = Ctx()


@pytest.fixture(autouse=True)
def _bind(sessions, pg_url, monkeypatch):
    from swfactory.settings import get_settings

    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    monkeypatch.delenv("FACTORY_DATABASE_PASSWORD_FILE", raising=False)
    get_settings.cache_clear()
    ctx.sessions, ctx.settings, ctx.deps = sessions, get_settings(), None
    real_build, real_graph = worker_mod.build_worker_deps, worker_mod.build_job_graph
    state: dict[str, Any] = {"exc": None, "after": None, "times": None, "calls": 0, "ran": False}
    ctx.state = state

    def build(*a, **kw):
        ctx.deps = real_build(*a, **kw)
        return ctx.deps

    class Broken:
        def __init__(self, graph):
            self._g = graph

        def __getattr__(self, name):
            return getattr(self._g, name)

        async def ainvoke(self, inp, cfg, **kw):
            st = state
            if st["exc"] is None or (st["times"] is not None and st["calls"] >= st["times"]):
                return await self._g.ainvoke(inp, cfg, **kw)
            st["calls"] += 1
            if st["after"] is not None and not st["ran"]:
                st["ran"] = True
                await self._g.ainvoke(inp, cfg, interrupt_after=[st["after"]], **kw)
            raise st["exc"]

    def graph(*a, **kw):
        return Broken(real_graph(*a, **kw))

    monkeypatch.setattr(worker_mod, "build_worker_deps", build)
    monkeypatch.setattr(worker_mod, "build_job_graph", graph)
    yield
    get_settings.cache_clear()


def break_graph(exc, after=None, times=None):
    ctx.state.update(exc=exc, after=after, times=times, calls=0, ran=False)


@pytest.fixture(autouse=True)
async def seeded(_bind):
    await seed_demo(ctx.sessions, defaults=FAST)
    return await enq(ctx.sessions, app="demo")


def work(fake, **kw):
    return run_worker(
        ctx.settings, worker_id="w1", fake_script=FIXTURES / fake, exit_when_idle=True, **kw
    )


def start(coro):
    return asyncio.ensure_future(coro)


async def job():
    async with ctx.sessions() as s:
        return await s.scalar(
            select(Job).order_by(Job.created_at).execution_options(populate_existing=True)
        )


async def events(kind):
    async with ctx.sessions() as s:
        return list(
            (
                await s.scalars(select(JobEvent).where(JobEvent.kind == kind).order_by(JobEvent.id))
            ).all()
        )


async def claim(w):
    return await claim_helper(ctx.sessions, w)


async def clear_backoff():
    async with ctx.sessions.begin() as s:
        await s.execute(text("update app.jobs set retry_after = NULL"))


async def set_crash_count(n):
    async with ctx.sessions.begin() as s:
        await s.execute(text("update app.jobs set crash_count = :n"), {"n": n})


async def db_now():
    async with ctx.sessions() as s:
        return await s.scalar(text("select now()"))


async def expire_lease():
    async with ctx.sessions.begin() as s:
        await s.execute(text("update app.jobs set lease_until = now() - interval '1 second'"))


async def steal_lease():
    async with ctx.sessions.begin() as s:
        await s.execute(
            text("update app.jobs set lease_owner = 'other', lease_epoch = lease_epoch + 1")
        )


async def running_row(role):
    for _ in range(200):
        async with ctx.sessions() as s:
            n = await s.scalar(
                select(AgentRun.id).where(AgentRun.role == role, AgentRun.status == "running")
            )
        if n is not None:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"no running {role} row")


async def ok_count(role):
    async with ctx.sessions() as s:
        return len(
            (
                await s.scalars(
                    select(AgentRun).where(AgentRun.role == role, AgentRun.status == "ok")
                )
            ).all()
        )


async def insert_cmd(action):
    j = await job()
    async with ctx.sessions.begin() as s:
        return await insert_command(
            s, job_id=j.id, action=action, note=None, requested_by="ann", gate_event_id=None
        )


async def cmd(cid):
    async with ctx.sessions() as s:
        return await s.scalar(
            select(Command).where(Command.id == cid).execution_options(populate_existing=True)
        )


async def crash_three_times(**kw):
    break_graph(RuntimeError("boom"), **kw)
    for _ in range(3):
        await work("happy.yaml")
        await clear_backoff()


async def test_escaping_exception_counts_a_crash_and_backs_off():
    break_graph(RuntimeError("checkpoint load failed"))
    await work("happy.yaml")
    j = await job()
    assert (j.crash_count, j.lease_owner, j.error) == (1, None, "checkpoint load failed")
    assert j.retry_after > await db_now() and await claim("w2") is None


async def test_third_crash_fails_the_job():
    break_graph(RuntimeError("checkpoint load failed"))
    for _ in range(3):
        await work("happy.yaml")
        await clear_backoff()
    j = await job()
    assert (j.stage, j.crash_count, j.lease_owner) == ("failed", 3, None)
    assert [e.data for e in await events("job.failed")] == [{"error": "checkpoint load failed"}]


async def test_expired_lease_counts_as_a_crash():
    j = await claim("w1")
    await expire_lease()
    again = await claim("w2")
    assert (again.id, again.crash_count) == (j.id, 1) and again.retry_after > await db_now()


async def test_third_expired_lease_fails_the_job():
    for w in ("w1", "w2", "w3"):
        await claim(w)
        await expire_lease()
        await clear_backoff()
    await work("happy.yaml")
    j = await job()
    assert (j.stage, j.crash_count, await ok_count("spec-writer")) == ("failed", 3, 0)
    assert "worker stopped 3 times" in j.error
    assert [e.data for e in await events("job.failed")] == [{"error": j.error}]


async def test_completed_node_resets_the_count():
    await set_crash_count(2)
    await work("happy.yaml")
    j = await job()
    assert (j.stage, j.crash_count, j.retry_after) == ("merged", 0, None)


async def test_lost_lease_is_not_a_crash():
    t = start(work("slow-build.yaml", renew_s=0.2, lease_s=3))
    await running_row("builder")
    await steal_lease()
    await asyncio.wait_for(t, 10)
    assert (await job()).crash_count == 0


async def test_retry_requeues_a_failed_job_from_its_checkpoint():
    await crash_three_times(after="build", times=3)
    assert (await job()).stage == "failed"
    cid = await insert_cmd("retry")
    await work("happy.yaml")
    j = await job()
    assert ((await cmd(cid)).result, j.stage, j.crash_count) == ("requeued", "merged", 0)
    assert await ok_count("builder") == 1  # build is not repeated


async def test_cancel_a_failed_job():
    await crash_three_times(after="build", times=3)
    cid = await insert_cmd("cancel")
    await work("happy.yaml")
    j = await job()
    assert ((await cmd(cid)).result, j.stage, ctx.deps.sandboxes.destroyed) == (
        "cancelled",
        "cancelled",
        [j.id],
    )


async def test_retry_refused_on_unsupported_graph_version(monkeypatch):
    from langgraph.graph.state import CompiledStateGraph

    j = await job()
    async with ctx.sessions.begin() as s:
        await s.execute(text("update app.jobs set graph_version = '0' where id = :i"), {"i": j.id})
    real = CompiledStateGraph.aget_state

    async def fake_state(self, config, **kw):
        snap = await real(self, config, **kw)
        return snap._replace(values={"job_id": "x"})

    monkeypatch.setattr(CompiledStateGraph, "aget_state", fake_state)
    await work("happy.yaml")
    assert (await job()).stage == "failed"
    cid = await insert_cmd("retry")
    await work("happy.yaml")
    assert ((await cmd(cid)).result, (await job()).stage) == (
        "refused: graph version no longer supported",
        "failed",
    )
    assert [e.data["error"] for e in await events("job.failed")] == [(await job()).error]
