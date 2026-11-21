"""The improver's ticker: trigger, cycles, leases, crash handling, resume, deadline, PR tracking."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, text
from tests.fakes.deps import make_test_deps
from tests.fakes.github import FakeGitHub
from tests.support.pg import truncate_app_tables
from tests.unit.improve.test_graph import (
    FakeBench,
    GhWorkspace,
    HostProvider,
    ImproverEngine,
    redraft,
)

from swfactory.db.tables import AgentRun, App, Improvement, Job, JobEvent
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.domain.types import Role
from swfactory.engines.fake import FakeScript
from swfactory.engines.registry import register_engine
from swfactory.improve import graph as improve_graph
from swfactory.improve.graph import (
    IMPROVER_BENCH_DEADLINE_H,
    ImproveDeps,
    ImproverTicker,
    build_improve_graph,
    improve_initial_state,
    improver_status,
    start_cycle,
)

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
MERGE_CALLS = ("merge_async", "merge_async_status", "enable_auto_merge", "mark_ready")


def _git(origin, *args):
    subprocess.run(
        ["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@x.io", *args],
        check=True,
        capture_output=True,
    )


class RecordingEngine(ImproverEngine):
    def __init__(self, name: str, log: list) -> None:
        super().__init__(name)
        self.log = log

    async def run(self, task, box):
        self.log.append(task)
        return await super().run(task, box)


async def build_world(sessions, tmp_path, *, enabled: bool):
    origin = tmp_path / "def"
    init_definition_repo(origin)
    if enabled:
        f = origin / "factory.yaml"
        f.write_text(f.read_text().replace("enabled: false", "enabled: true", 1))
        _git(origin, "commit", "-qam", "enable the improver")
    result = load_definition(str(origin))
    assert result.definition is not None
    core = make_test_deps(sessions, FakeScript())
    core = replace(core, sandboxes=HostProvider(tmp_path / "boxes"))  # pyright: ignore
    assert await core.definitions.save(result)
    tasks: list = []
    register_engine(RecordingEngine("fake", tasks))
    register_engine(RecordingEngine("direct", tasks))
    bench = FakeBench(sessions)
    await bench.add_run("v1-parity", result.sha, "done")
    bench.started_base = ("v1-parity", result.sha)
    gh = FakeGitHub()
    cp = InMemorySaver()
    ws_job = UUID(int=7)
    deps = ImproveDeps(
        core=core,
        bench=bench,  # pyright: ignore
        workspace=lambda: GhWorkspace(
            str(origin), tmp_path / "ws", job_id=ws_job, auth=None, gitops=None
        ),
        github=lambda slug: gh,  # pyright: ignore
        clock=lambda: NOW,
        checkpointer=cp,
    )
    return SimpleNamespace(
        sessions=sessions,
        origin=origin,
        base_sha=result.sha,
        defn=result.definition,
        core=core,
        deps=deps,
        bench=bench,
        gh=gh,
        cp=cp,
        tasks=tasks,
        ticker=ImproverTicker(deps, worker_id="w1", lease_s=90, renew_s=30),
        seq=0,
    )


async def teardown(w, sessions, tmp_path):
    for name in MERGE_CALLS:  # the improver never touched a merge API
        assert w.gh.calls[name] == 0
    async with sessions() as s:
        conn = await s.connection()
        await truncate_app_tables(conn)
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
async def world(sessions, tmp_path):
    w = await build_world(sessions, tmp_path, enabled=True)
    yield w
    await teardown(w, sessions, tmp_path)


@pytest.fixture
async def world_off(sessions, tmp_path):
    w = await build_world(sessions, tmp_path, enabled=False)
    yield w
    await teardown(w, sessions, tmp_path)


async def add_failures(w, builder=0, security=0, at=NOW - timedelta(hours=1)):
    async with w.sessions.begin() as s:
        app_id = await s.scalar(select(App.id))
        for role, n in (("builder", builder), ("reviewer-security", security)):
            for _ in range(n):
                w.seq += 1
                j = Job(
                    app_id=app_id,
                    seq=w.seq,
                    slug=f"j{w.seq}",
                    title="t",
                    feature="f",
                    requested_by="me",
                    branch=f"b{w.seq}",
                    budget_usd=Decimal("1"),
                    definition_sha=w.base_sha,
                    stage="failed",
                    finished_at=at,
                    gate_reason="step failed: build"
                    if role == "builder"
                    else "step failed: review",
                )
                s.add(j)
                await s.flush()
                if role != "builder":
                    s.add(AgentRun(job_id=j.id, role=role, round=1, engine="e", status="failed"))


async def rows(w, ids) -> list[Improvement]:
    async with w.sessions() as s:
        found = list(await s.scalars(select(Improvement).where(Improvement.id.in_(ids))))
    return sorted(found, key=lambda r: (r.created_at, r.id))


async def row(w, imp_id) -> Improvement:
    async with w.sessions() as s:
        found = await s.get(Improvement, imp_id)
    assert found is not None
    return found


async def all_rows(w) -> list[Improvement]:
    async with w.sessions() as s:
        return list(await s.scalars(select(Improvement).order_by(Improvement.created_at)))


async def status_events(w, imp_id) -> list[str]:
    async with w.sessions() as s:
        evs = list(await s.scalars(select(JobEvent).order_by(JobEvent.id)))
    return [
        e.data["status"]
        for e in evs
        if e.kind == "improvement.updated" and e.data["id"] == str(imp_id)
    ]


async def job_of(w, imp_id) -> Job:
    async with w.sessions() as s:
        r = await s.get(Improvement, imp_id)
        assert r is not None and r.job_id is not None
        j = await s.get(Job, r.job_id)
    assert j is not None
    return j


async def manual_cycle(w, role=None) -> list[UUID]:
    async with w.sessions.begin() as s:
        return await start_cycle(s, w.defn, trigger="manual", role=role, now=NOW)


def improver_tasks(w, round_):
    return [t for t in w.tasks if t.role == Role.IMPROVER and t.round == round_]


def set_edits(edits):
    from swfactory.engines.registry import get_engine

    for name in ("fake", "direct"):
        get_engine(name).edits = edits  # pyright: ignore


async def test_cycle_at_threshold(world):
    await add_failures(world, builder=15, security=10)
    ids = await world.ticker.tick(NOW)
    found = await rows(world, ids)
    assert [r.role_group for r in found] == ["builder", "reviewer-security"]
    assert {r.trigger for r in found} == {"threshold"}
    async with world.sessions() as s:
        jobs = list(await s.scalars(select(Job).where(Job.kind == "improver")))
    assert len(jobs) == 2 and all(j.app_id is None for j in jobs)
    assert {r.base_sha for r in found} == {world.base_sha} and {r.suite for r in found} == {
        "v1-parity"
    }


async def test_24_fresh_failures_do_nothing(world):
    await add_failures(world, builder=24)
    assert await world.ticker.tick(NOW) == []
    assert await all_rows(world) == []


async def test_age_trigger(world):
    await add_failures(world, builder=1, at=NOW - timedelta(days=7))
    ids = await world.ticker.tick(NOW)
    assert len(ids) == 1 and (await rows(world, ids))[0].trigger == "age"


async def test_cycle_skips_groups_with_an_open_improvement(world):
    await add_failures(world, builder=15, security=10)
    async with world.sessions.begin() as s:
        s.add(
            Improvement(
                trigger="manual",
                role_group="builder",
                status="pr_open",
                base_sha=world.base_sha,
                suite="v1-parity",
                created_at=NOW - timedelta(days=30),
            )
        )
    ids = await world.ticker.tick(NOW)
    assert [r.role_group for r in await rows(world, ids)] == ["reviewer-security"]


async def test_cycle_rows_share_one_cycle_id(world):
    await add_failures(world, builder=15, security=10)
    ids = await world.ticker.tick(NOW)
    found = await rows(world, ids)
    assert len(ids) == 2 and len({r.cycle_id for r in found}) == 1 and found[0].cycle_id is not None


async def test_resets_after_improvement(world):
    await add_failures(world, builder=15, security=10)
    assert await world.ticker.tick(NOW)
    assert await world.ticker.tick(NOW + timedelta(minutes=1)) == []


async def test_disabled_ticks_nothing_but_manual_run_works(world_off):
    w = world_off
    await add_failures(w, builder=15, security=10)
    assert await w.ticker.tick(NOW) == []
    async with w.sessions.begin() as s:
        assert len(await start_cycle(s, w.defn, trigger="manual")) == 2


async def test_manual_run_for_one_role_and_without_a_suite(world):
    await add_failures(world, builder=15, security=10)
    ids = await manual_cycle(world, role=Role.BUILDER)
    assert [r.role_group for r in await rows(world, ids)] == ["builder"]
    cfg = world.defn.factory.improver.model_copy(update={"bench_suite": None})
    factory = world.defn.factory.model_copy(update={"improver": cfg})
    nosuite = world.defn.model_copy(update={"factory": factory})
    async with world.sessions.begin() as s:
        with pytest.raises(ValueError, match="set improver.bench_suite first"):
            await start_cycle(s, nosuite, trigger="manual")


async def test_status_counts_failures_against_the_threshold(world):
    await add_failures(world, builder=15, security=12)
    async with world.sessions() as s:
        st = await improver_status(s, world.defn, now=NOW)
    assert (st["failures"], st["threshold"], st["due"]) == (27, 25, "threshold")


async def test_one_improvement_benchmarking_at_a_time(world):
    await add_failures(world, builder=15, security=10)
    set_edits({"roles/builder.md": redraft})
    await world.ticker.tick(NOW)
    await world.ticker.tick(NOW + timedelta(hours=1))
    assert [r.status for r in await all_rows(world)] == ["benchmarking", "queued"]


async def test_resumes_when_bench_done_and_tracks_merge(world):
    w = world
    await add_failures(w, builder=15, security=10)
    set_edits({"roles/builder.md": redraft})
    ids = await w.ticker.tick(NOW)
    a, b = [r.id for r in await rows(w, ids)]
    assert (await row(w, a)).status == "benchmarking"
    for k in w.bench.state:
        w.bench.state[k] = "done"
    await w.ticker.tick(NOW + timedelta(hours=1))
    ra = await row(w, a)
    assert ra.status == "pr_open" and ra.branch
    assert (await row(w, b)).status == "queued"  # waits for the next tick
    w.gh.merge_externally(1)
    set_edits({"roles/reviewer-security.md": redraft})  # the second group runs in turn
    await w.ticker.tick(NOW + timedelta(hours=2))
    assert (await status_events(w, a))[-2:] == ["pr_open", "merged"]
    assert (await row(w, b)).status == "benchmarking", (await row(w, b)).evidence
    for k in w.bench.state:
        w.bench.state[k] = "done"
    await w.ticker.tick(NOW + timedelta(hours=3))
    assert (await row(w, b)).status == "pr_open"
    w.gh.prs[2]["state"] = "closed"
    await w.ticker.tick(NOW + timedelta(hours=4))
    assert (await status_events(w, b))[-2:] == ["pr_open", "closed"]
    assert (await row(w, b)).status == "closed"


async def test_two_workers_never_run_the_same_improvement(world):
    w = world
    await add_failures(w, builder=1)
    [imp] = await manual_cycle(w)
    set_edits({"roles/builder.md": redraft})
    t1 = ImproverTicker(w.deps, worker_id="w1")
    t2 = ImproverTicker(w.deps, worker_id="w2")
    await asyncio.gather(t1.tick(NOW), t2.tick(NOW))
    assert (await status_events(w, imp)).count("analyzing") == 1
    assert len(improver_tasks(w, 1)) == 1
    assert (await job_of(w, imp)).lease_epoch == 1


async def _checkpoint_after_analyze(w, imp_id):
    r = await row(w, imp_id)
    g = build_improve_graph(w.deps, w.cp)
    cfg: Any = {"configurable": {"thread_id": str(imp_id)}}
    await g.ainvoke(improve_initial_state(r), cfg, interrupt_after=["analyze"])
    return r


async def test_reclaims_crashed_improvement(world):
    w = world
    await add_failures(w, builder=1)
    [imp] = await manual_cycle(w)
    set_edits({"roles/builder.md": redraft})
    r = await _checkpoint_after_analyze(w, imp)
    assert len(improver_tasks(w, 1)) == 1
    w.tasks.clear()
    async with w.sessions.begin() as s:
        await s.execute(
            text("update app.improvements set status = 'proposing' where id = :i"),
            {"i": imp},
        )
        await s.execute(
            text(
                "update app.jobs set lease_owner = 'dead', lease_epoch = 1, "
                "lease_until = :t where id = :j"
            ),
            {"t": NOW - timedelta(minutes=5), "j": r.job_id},
        )
    await w.ticker.tick(NOW)
    assert (await row(w, imp)).status == "benchmarking"
    assert improver_tasks(w, 1) == []  # a finished node does not run again
    job = await job_of(w, imp)
    assert job.lease_owner is None and job.lease_epoch == 2


async def test_poisoned_improvement_fails_instead_of_being_reclaimed(world):
    w = world
    await add_failures(w, builder=1)
    [imp] = await manual_cycle(w)
    async with w.sessions.begin() as s:
        await s.execute(
            text("update app.improvements set status = 'analyzing' where id = :i"),
            {"i": imp},
        )
        await s.execute(
            text(
                "update app.jobs set lease_owner = 'dead', lease_epoch = 3, crash_count = 2, "
                "lease_until = :t where id = (select job_id from app.improvements where id = :i)"
            ),
            {"t": NOW - timedelta(minutes=5), "i": imp},
        )
    await w.ticker.tick(NOW)
    r = await row(w, imp)
    assert r.status == "failed" and "stopped 3 times" in (r.error or "")
    job = await job_of(w, imp)
    assert job.stage == "failed" and job.lease_owner is None
    async with w.sessions() as s:
        kinds = list(await s.scalars(select(JobEvent.kind).where(JobEvent.job_id == job.id)))
    assert "job.failed" in kinds
    assert improver_tasks(w, 1) == []


class _Boom:
    async def aget_state(self, cfg):
        return SimpleNamespace(values={}, next=())

    async def ainvoke(self, *a, **kw):
        raise RuntimeError("boom")


async def test_escaping_exception_counts_a_crash_with_backoff(world, monkeypatch):
    w = world
    await add_failures(w, builder=1)
    [imp] = await manual_cycle(w)
    monkeypatch.setattr(improve_graph, "build_improve_graph", lambda *a, **k: _Boom())
    await w.ticker.tick(NOW)
    job = await job_of(w, imp)
    assert job.crash_count == 1 and job.lease_owner is None and "boom" in (job.error or "")
    assert job.retry_after == NOW + timedelta(seconds=60)
    await w.ticker.tick(NOW + timedelta(seconds=30))  # still backing off
    assert (await job_of(w, imp)).crash_count == 1
    await w.ticker.tick(NOW + timedelta(seconds=61))
    job = await job_of(w, imp)
    assert job.crash_count == 2 and job.retry_after == NOW + timedelta(seconds=61 + 120)
    await w.ticker.tick(NOW + timedelta(seconds=61 + 121))
    r = await row(w, imp)
    assert r.status == "failed" and "boom" in (r.error or "")
    assert (await job_of(w, imp)).stage == "failed"


async def test_benchmarking_deadline(world):
    w = world
    await add_failures(w, builder=1)
    [imp] = await manual_cycle(w)
    run_id = await w.bench.add_run("v1-parity", "b" * 40, "running")
    async with w.sessions.begin() as s:
        await s.execute(
            text(
                "update app.improvements set status = 'benchmarking', bench_run_before = :r, "
                "bench_run_after = :r, updated_at = :t where id = :i"
            ),
            {"r": run_id, "t": NOW - timedelta(hours=IMPROVER_BENCH_DEADLINE_H + 1), "i": imp},
        )
    await w.ticker.tick(NOW)
    r = await row(w, imp)
    assert (r.status, r.error) == ("failed", "benchmark did not finish in 48 h")
    assert (await job_of(w, imp)).stage == "closed"


async def test_a_failed_bench_run_fails_the_improvement(world):
    w = world
    await add_failures(w, builder=1)
    [imp] = await manual_cycle(w)
    set_edits({"roles/builder.md": redraft})
    await w.ticker.tick(NOW)
    assert (await row(w, imp)).status == "benchmarking"
    for k in w.bench.state:
        w.bench.state[k] = "failed"
    await w.ticker.tick(NOW + timedelta(hours=1))
    r = await row(w, imp)
    assert r.status == "failed" and r.error == "a benchmark run did not finish"


async def test_cli_against_the_real_database(world, monkeypatch):
    """`factory improve status|run|list` through the real CLI path and a real database."""
    from typer.testing import CliRunner

    from swfactory.cli.main import app
    from swfactory.settings import get_settings

    url = world.sessions.kw["bind"].url.render_as_string(hide_password=False)
    monkeypatch.setenv("FACTORY_DATABASE_URL", url)
    monkeypatch.delenv("FACTORY_DATABASE_PASSWORD_FILE", raising=False)
    get_settings.cache_clear()
    await add_failures(world, builder=15, security=12)
    loop = asyncio.get_running_loop()

    async def cli(*args):
        return await loop.run_in_executor(None, lambda: CliRunner().invoke(app, list(args)))

    out = (await cli("improve", "status")).output
    assert "failures since last improvement: 27 of 25" in out and "next trigger: threshold" in out
    r = await cli("improve", "run", "--role", "builder")
    assert r.exit_code == 0 and "queued improvement" in r.output and "builder" in r.output
    assert (await cli("improve", "run", "--role", "builder")).output.startswith(
        "nothing to improve"
    )
    listing = (await cli("improve", "list", "--limit", "5")).output
    assert "builder manual queued -" in listing
    out = (await cli("improve", "status")).output
    assert "in progress: builder (queued)" in out
