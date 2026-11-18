import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from tests.fakes.deps import DEFAULT_TIMEOUTS, seed_demo
from tests.support.db_helpers import claim as claim_helper
from tests.support.db_helpers import enq
from typer.testing import CliRunner

import swfactory.graph.build as build_mod
import swfactory.graph.nodes as nodes_mod
import swfactory.worker as worker_mod
from swfactory.cli.main import app
from swfactory.db.repo import holds_lease
from swfactory.db.tables import AgentRun, Job, JobEvent
from swfactory.queue import claim_next_job, release_job, renew_lease
from swfactory.worker import run_worker

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parents[1] / "fixtures" / "fake"
FAST = {"engine_retry_backoff_s": [0, 0]}


class Ctx:
    sessions: Any = None
    settings: Any = None
    deps: Any = None
    renew_log: list[tuple[str, float]] = []
    drive_ended: list[float] = []
    break_renew = False


ctx = Ctx()


@pytest.fixture(autouse=True)
def _bind(sessions, pg_url, monkeypatch):
    from swfactory.settings import get_settings

    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    get_settings.cache_clear()
    ctx.sessions = sessions
    ctx.settings = get_settings()
    ctx.deps = None
    ctx.renew_log, ctx.drive_ended, ctx.break_renew = [], [], False
    real_build, real_renew, real_drive = (
        worker_mod.build_worker_deps,
        worker_mod.renew_lease,
        worker_mod.drive_job,
    )

    def build(*a, **kw):
        ctx.deps = real_build(*a, **kw)
        return ctx.deps

    async def renew(*a, **kw):
        if ctx.break_renew:
            from sqlalchemy.exc import OperationalError

            raise OperationalError("x", {}, Exception("db down"))
        ok = await real_renew(*a, **kw)
        loop = asyncio.get_running_loop()
        ctx.renew_log.append(("ok" if ok else "lost", loop.time()))
        return ok

    async def drive(*a, **kw):
        try:
            return await real_drive(*a, **kw)
        finally:
            ctx.drive_ended.append(asyncio.get_running_loop().time())

    monkeypatch.setattr(worker_mod, "build_worker_deps", build)
    monkeypatch.setattr(worker_mod, "renew_lease", renew)
    monkeypatch.setattr(worker_mod, "drive_job", drive)
    yield
    get_settings.cache_clear()


def script(tmp_path, latency=0.6):
    p = tmp_path / "short-build.yaml"
    p.write_text(f"turns:\n  - {{role: builder, round: 1, attempt: 1, latency_s: {latency}}}\n")
    return p


def work(fake, **kw):
    path = fake if isinstance(fake, Path) else FIXTURES / fake
    return run_worker(ctx.settings, worker_id="w1", fake_script=path, exit_when_idle=True, **kw)


def start(coro):
    return asyncio.ensure_future(coro)


async def jobs():
    async with ctx.sessions() as s:
        return list(
            (
                await s.scalars(
                    select(Job).order_by(Job.created_at).execution_options(populate_existing=True)
                )
            ).all()
        )


async def job():
    return (await jobs())[0]


async def events(kind):
    async with ctx.sessions() as s:
        return list(
            (
                await s.scalars(select(JobEvent).where(JobEvent.kind == kind).order_by(JobEvent.id))
            ).all()
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


async def steal_lease():
    async with ctx.sessions.begin() as s:
        await s.execute(
            text("update app.jobs set lease_owner = 'other', lease_epoch = lease_epoch + 1")
        )


async def expire_lease():
    async with ctx.sessions.begin() as s:
        await s.execute(text("update app.jobs set lease_until = now() - interval '1 second'"))


async def seeded(**kw):
    kw.setdefault("defaults", FAST)
    await seed_demo(ctx.sessions, **kw)
    return await enq(ctx.sessions, app="demo")


async def max_concurrent(coro):
    peak = 0
    t = start(coro)
    while not t.done():
        async with ctx.sessions() as s:
            n = await s.scalar(text("select count(*) from app.jobs where lease_owner is not null"))
        peak = max(peak, n or 0)
        await asyncio.sleep(0.1)
    await t
    return peak


async def ok_count(role):
    async with ctx.sessions() as s:
        return len(
            (
                await s.scalars(
                    select(AgentRun).where(AgentRun.role == role, AgentRun.status == "ok")
                )
            ).all()
        )


async def test_runs_queued_job_and_records_graph_version():
    await seeded()
    await work("happy.yaml")
    j = await job()
    assert (j.stage, j.graph_version, j.lease_owner) == ("merged", "1", None)


async def test_interrupt_sets_needs_human_and_releases():
    await seeded()
    await work("escalate.yaml")
    j = await job()
    assert (j.stage, j.gate_reason, j.lease_owner) == ("needs_human", "approver escalated", None)
    (ev,) = await events("human.waiting")
    assert ev.data["reason"] == "approver escalated" and ev.data["retry_node"] is None
    assert ev.data["artifacts"][0] == "decision.md"


async def test_drive_job_binds_job_id(capsys):
    import json

    from swfactory.logging import configure_logging

    configure_logging()
    await seeded()
    await work("happy.yaml")
    cap = capsys.readouterr()
    out = cap.out + cap.err
    ids = set()
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("event") == "fake.turn":
            ids.add(d.get("job_id"))
    assert ids == {str((await job()).id)}


async def test_lease_is_renewed():
    await seeded()
    t = start(work("slow-build.yaml", renew_s=0.2, lease_s=3))
    await running_row("builder")
    u1 = (await job()).lease_until
    await asyncio.sleep(0.5)
    j = await job()
    assert j.lease_until > u1 and j.lease_owner == "w1"
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)


async def test_lost_lease_cancels_job_task():
    await seeded()
    t = start(work("slow-build.yaml", renew_s=0.2, lease_s=3))
    await running_row("builder")
    await steal_lease()
    await asyncio.wait_for(t, 10)
    j = await job()
    assert (j.lease_owner, j.stage) == ("other", "build")


async def test_claim_increments_lease_epoch():
    await seeded()
    j1 = await claim_helper(ctx.sessions, "w1")
    await expire_lease()
    j2 = await claim_helper(ctx.sessions, "w2")
    assert (j1.lease_epoch, j2.lease_epoch) == (1, 2)


async def test_renew_and_release_match_the_epoch():
    await seeded()
    j1 = await claim_helper(ctx.sessions, "w1")
    await expire_lease()
    j2 = await claim_helper(ctx.sessions, "w1")
    async with ctx.sessions() as s:
        assert (
            await renew_lease(
                s, job_id=j1.id, worker_id="w1", lease_epoch=j1.lease_epoch, lease_s=90
            )
            is False
        )
    async with ctx.sessions() as s:
        await release_job(s, job_id=j1.id, worker_id="w1", lease_epoch=j1.lease_epoch)
    j = await job()
    assert (j.lease_owner, j.lease_epoch) == ("w1", j2.lease_epoch)


async def test_claim_never_returns_an_excluded_job():
    await seeded()
    j = await claim_helper(ctx.sessions, "w1")
    await expire_lease()
    async with ctx.sessions() as s:
        assert await claim_next_job(s, worker_id="w1", lease_s=90, exclude={j.id}) is None
    async with ctx.sessions() as s:
        assert (await claim_next_job(s, worker_id="w1", lease_s=90)) is not None


async def test_holds_lease_checks_owner_and_epoch():
    await seeded()
    j = await claim_helper(ctx.sessions, "w1")
    async with ctx.sessions.begin() as s:
        assert await holds_lease(s, job_id=j.id, worker_id="w1", lease_epoch=j.lease_epoch)
        assert not await holds_lease(s, job_id=j.id, worker_id="w2", lease_epoch=j.lease_epoch)
        assert not await holds_lease(s, job_id=j.id, worker_id="w1", lease_epoch=j.lease_epoch + 1)


async def test_lease_must_cover_three_renewals():
    with pytest.raises(ValueError, match="lease_s must be at least 3 x renew_s"):
        await run_worker(ctx.settings, worker_id="w1", lease_s=60, renew_s=30)


async def test_renewal_errors_stop_the_task_before_the_lease_expires():
    await seeded()
    t = start(work("slow-build.yaml", renew_s=0.2, lease_s=3))
    await running_row("builder")
    await asyncio.sleep(0.5)
    ctx.break_renew = True
    await asyncio.sleep(4)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    last_good = max(at for ok, at in ctx.renew_log if ok == "ok")
    assert (
        ctx.drive_ended and ctx.drive_ended[0] - last_good < 3.0
    )  # before the 3 s lease would expire


async def test_no_checkpoint_starts_fresh(monkeypatch):
    from langgraph.graph.state import CompiledStateGraph

    await seeded()
    real = CompiledStateGraph.aget_state

    async def fake_state(self, config, **kw):
        snap = await real(self, config, **kw)
        return snap._replace(values={"job_id": "x"})

    monkeypatch.setattr(CompiledStateGraph, "aget_state", fake_state)
    await work("happy.yaml")
    assert ((await job()).stage, await ok_count("spec-writer")) == ("merged", 1)


def lose_lease_at(monkeypatch, node):
    real = build_mod.holds_lease
    entered = {"at": False}

    async def fake(s, **kw):
        if entered["at"]:
            return False
        return await real(s, **kw)

    monkeypatch.setattr(build_mod, "holds_lease", fake)
    monkeypatch.setattr(nodes_mod, "holds_lease", fake)
    orig = build_mod._leased

    def leased(deps, fn):
        inner = orig(deps, fn)

        async def wrapper(state, config):
            if fn.__name__ == node:
                entered["at"] = True
            return await inner(state, config)

        wrapper.__name__ = fn.__name__
        return wrapper

    monkeypatch.setattr(build_mod, "_leased", leased)


async def test_lease_lost_inside_a_node_is_raised_not_gated(monkeypatch):
    await seeded()
    lose_lease_at(monkeypatch, "deliver")
    await work("happy.yaml")
    j = await job()
    assert (await events("human.waiting"), j.gate_reason) == ([], None)
    assert "deliver" not in ctx.deps.git.calls


async def test_graph_end_without_a_stage_write_sets_the_outcome(monkeypatch):
    await seeded()
    real = nodes_mod.set_stage

    async def broken(s, job_id, stage, **kw):
        if stage.value == "merged":
            raise RuntimeError("boom")
        return await real(s, job_id, stage, **kw)

    monkeypatch.setattr(nodes_mod, "set_stage", broken)
    await work("happy.yaml")
    j = await job()
    assert (j.stage, j.lease_owner) == ("merged", None)


async def test_graph_end_without_outcome_fails_the_job():
    await seeded()
    claimed = await claim_helper(ctx.sessions, "w1")

    class D:
        sessions = ctx.sessions

    await worker_mod._settle(D(), claimed, {}, "w1", claimed.lease_epoch)  # type: ignore[arg-type]
    j = await job()
    assert (j.stage, j.error) == ("failed", "graph ended without an outcome")


async def seed_checkpoint(j, graph_version):
    async with ctx.sessions.begin() as s:
        await s.execute(
            text("update app.jobs set graph_version = :v where id = :i"),
            {"v": str(graph_version), "i": j.id},
        )


async def test_graph_version_mismatch_fails_job(monkeypatch):
    from langgraph.graph.state import CompiledStateGraph

    j = await seeded()
    await seed_checkpoint(j, 0)
    real = CompiledStateGraph.aget_state

    async def fake_state(self, config, **kw):
        snap = await real(self, config, **kw)
        return snap._replace(values={"job_id": "x"})

    monkeypatch.setattr(CompiledStateGraph, "aget_state", fake_state)
    await work("happy.yaml")
    j2 = await job()
    assert j2.stage == "failed" and "graph version" in (j2.error or "")


async def test_global_concurrency_from_definition(tmp_path):
    await seed_demo(
        ctx.sessions, apps=("a", "b"), concurrency={"global": 1, "per_app": 1}, defaults=FAST
    )
    await enq(ctx.sessions, app="a")
    await enq(ctx.sessions, app="b")
    assert await max_concurrent(work(script(tmp_path))) == 1
    assert {j.stage for j in await jobs()} == {"merged"}


async def test_max_jobs_override(tmp_path):
    await seed_demo(
        ctx.sessions, apps=("a", "b"), concurrency={"global": 1, "per_app": 1}, defaults=FAST
    )
    await enq(ctx.sessions, app="a")
    await enq(ctx.sessions, app="b")
    assert await max_concurrent(work(script(tmp_path, 1.0), max_jobs=2)) == 2


async def test_definition_reload_raises_the_limit(tmp_path):
    await seed_demo(
        ctx.sessions, apps=("a", "b"), concurrency={"global": 1, "per_app": 1}, defaults=FAST
    )
    await enq(ctx.sessions, app="a")
    await enq(ctx.sessions, app="b")

    async def work_then_activate():
        t = start(work(script(tmp_path, 3.0)))
        await running_row("builder")
        await seed_demo(
            ctx.sessions, apps=("a", "b"), concurrency={"global": 2, "per_app": 1}, defaults=FAST
        )
        await t

    assert await max_concurrent(work_then_activate()) == 2
    assert {j.stage for j in await jobs()} == {"merged"}


async def test_definition_activated_mid_job_does_not_change_running_job(tmp_path, monkeypatch):
    from swfactory.sandbox.fake import FakeSandboxProvider

    seen = []
    real_ensure = FakeSandboxProvider.ensure

    async def ensure(self, job_id, runner):
        box = await real_ensure(self, job_id, runner)
        seen.append(box)
        return box

    monkeypatch.setattr(FakeSandboxProvider, "ensure", ensure)
    pinned = await seed_demo(
        ctx.sessions,
        app={"test_command": "make test", "max_rounds": 2},
        defaults=FAST,
    )
    await enq(ctx.sessions, app="demo")
    t = start(work(script(tmp_path, 1.0)))
    await running_row("builder")
    await seed_demo(
        ctx.sessions,
        app={"test_command": "make other", "max_rounds": 1, "max_fix_attempts": 0},
        defaults={**FAST, "step_timeouts_min": {**DEFAULT_TIMEOUTS, "verify": 1}},
    )
    await t
    j = await job()
    assert (j.stage, j.definition_sha) == ("merged", pinned)
    calls = [c for b in seen for c in b.exec_calls]
    assert ["sh", "-c", "make test"] in calls and ["sh", "-c", "make other"] not in calls
    box = seen[0]
    assert box.exec_timeouts[box.exec_calls.index(["sh", "-c", "make test"])] == 900


def test_cli_worker_needs_a_fake_script():
    r = CliRunner().invoke(app, ["worker"])
    assert r.exit_code == 2 and "no engines yet: pass --fake-script" in r.output


def test_cli_worker_rejects_short_lease():
    r = CliRunner().invoke(
        app,
        [
            "worker",
            "--fake-script",
            str(FIXTURES / "happy.yaml"),
            "--renew-s",
            "30",
            "--lease-s",
            "60",
        ],
    )
    assert r.exit_code == 2 and "lease_s must be at least 3 x renew_s" in r.output
