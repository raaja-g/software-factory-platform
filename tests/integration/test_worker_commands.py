import asyncio
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq
from typer.testing import CliRunner

import swfactory.worker as worker_mod
from swfactory.cli.main import app
from swfactory.commands import to_resume
from swfactory.db.engine import DURABILITY, open_checkpointer
from swfactory.db.repo import insert_command, latest_waiting
from swfactory.db.tables import AgentRun, Command, Job, JobEvent
from swfactory.domain.types import HumanAction
from swfactory.gitops.fake import FakeGitService
from swfactory.graph.build import build_job_graph
from swfactory.graph.steps import pinned_definition
from swfactory.worker import run_worker

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parents[1] / "fixtures" / "fake"
FAST = {"engine_retry_backoff_s": [0, 0]}


class Ctx:
    sessions: Any = None
    settings: Any = None
    deps: Any = None
    on_claim: Any = None


ctx = Ctx()


@pytest.fixture(autouse=True)
def _bind(sessions, pg_url, monkeypatch):
    from swfactory.settings import get_settings

    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    monkeypatch.delenv("FACTORY_DATABASE_PASSWORD_FILE", raising=False)
    get_settings.cache_clear()
    ctx.sessions, ctx.settings, ctx.deps, ctx.on_claim = sessions, get_settings(), None, None
    real_build, real_drive = worker_mod.build_worker_deps, worker_mod.drive_job

    def build(*a, **kw):
        ctx.deps = real_build(*a, **kw)
        return ctx.deps

    async def drive(*a, **kw):
        if ctx.on_claim is not None:
            await ctx.on_claim()
        return await real_drive(*a, **kw)

    monkeypatch.setattr(worker_mod, "build_worker_deps", build)
    monkeypatch.setattr(worker_mod, "drive_job", drive)
    yield
    get_settings.cache_clear()


@pytest.fixture
async def seeded():
    await seed_demo(ctx.sessions, defaults=FAST)
    return await enq(ctx.sessions, app="demo")


def work(fake, on_claim=None, **kw):
    ctx.on_claim = on_claim
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


async def waiting_id():
    async with ctx.sessions() as s:
        found = await latest_waiting(s, (await job()).id)
    assert found is not None
    return found[0]


async def insert_cmd(action, note=None, gate_event_id: Any = "auto"):
    j = await job()
    if gate_event_id == "auto":
        gate_event_id = await waiting_id() if j.stage == "needs_human" else None
    async with ctx.sessions.begin() as s:
        return await insert_command(
            s,
            job_id=j.id,
            action=action,
            note=note,
            requested_by="ann",
            gate_event_id=gate_event_id,
        )


async def cmd(cid):
    async with ctx.sessions() as s:
        return await s.scalar(
            select(Command).where(Command.id == cid).execution_options(populate_existing=True)
        )


async def commands_rows():
    async with ctx.sessions() as s:
        return list((await s.scalars(select(Command).order_by(Command.created_at))).all())


async def rows(role):
    async with ctx.sessions() as s:
        return list(
            (
                await s.scalars(
                    select(AgentRun).where(AgentRun.role == role).order_by(AgentRun.attempt)
                )
            ).all()
        )


async def run_count():
    async with ctx.sessions() as s:
        return await s.scalar(select(func.count()).select_from(AgentRun))


async def count_events(kind):
    async with ctx.sessions() as s:
        return await s.scalar(select(func.count()).where(JobEvent.kind == kind))


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


async def bump_epoch():
    async with ctx.sessions.begin() as s:
        await s.execute(text("update app.jobs set lease_epoch = lease_epoch + 1"))


async def resume_without_consuming(cid):
    """Answer the wait through the graph itself, as a worker that crashed before the consume."""
    j = await job()
    c = await cmd(cid)
    _, defn, _ = await pinned_definition(ctx.deps, j.id)
    resume = to_resume(
        HumanAction(c.action), note=c.note, by=c.requested_by, retry_offered=False, command_id=c.id
    )
    async with open_checkpointer(ctx.settings.sqlalchemy_url()) as cp:
        graph = build_job_graph(ctx.deps, cp)
        await graph.ainvoke(
            resume, {"configurable": {"thread_id": str(j.id)}}, durability=DURABILITY
        )
    async with ctx.sessions.begin() as s:
        await s.execute(text("update app.jobs set stage = 'verify'"))  # the crash left it mid-run


@pytest.fixture
async def cli(api_env):
    loop = asyncio.get_running_loop()

    async def call(*args):
        return await loop.run_in_executor(None, lambda: CliRunner().invoke(app, list(args)))

    return call


def outcome(r):
    return r.exit_code, r.output.strip().splitlines()[-1]


async def test_rework_command_resumes_and_is_consumed(seeded):
    await work("escalate.yaml")
    cid = await insert_cmd("rework", note="Add a test")
    await work("escalate.yaml")
    c = await cmd(cid)
    assert (c.result, c.consumed_at is not None, (await job()).stage) == ("resumed", True, "merged")


async def test_command_already_applied_is_consumed_without_resume(seeded):
    await work("escalate.yaml")
    cid = await insert_cmd("approve")
    await resume_without_consuming(cid)
    await work("escalate.yaml")
    assert ((await cmd(cid)).result, (await job()).stage, await count_events("human.action")) == (
        "applied",
        "merged",
        1,
    )


async def test_retry_not_offered_is_refused(seeded):
    await work("escalate.yaml")
    n = await run_count()
    cid = await insert_cmd("retry")
    await work("escalate.yaml")
    assert ((await cmd(cid)).result, (await job()).stage, await run_count()) == (
        "refused: retry not offered",
        "needs_human",
        n,
    )


async def test_retry_offered_resumes(seeded):
    await work("reviewer-error.yaml")
    cid = await insert_cmd("retry")
    await work("reviewer-error.yaml")
    assert ((await cmd(cid)).result, [r.attempt for r in await rows("reviewer-ux")]) == (
        "resumed",
        [1, 2],
    )


async def test_cancel_stops_running_job(seeded):
    t = start(work("slow-build.yaml"))
    await running_row("builder")
    await insert_cmd("cancel")
    await asyncio.wait_for(t, 5)
    assert ((await job()).stage, (await rows("builder"))[-1].status) == ("cancelled", "cancelled")
    assert (ctx.deps.git.calls[-1], ctx.deps.sandboxes.destroyed) == (
        "keep_branch",
        [(await job()).id],
    )
    assert (await job()).lease_owner is None


async def deliver_job():
    """Leave the job in stage delivered with an open PR, as the delivery step does."""
    async with ctx.sessions.begin() as s:
        await s.execute(
            text(
                "update app.jobs set stage = 'delivered', "
                "pr_url = 'https://github.com/acme/web/pull/1'"
            )
        )


async def test_cancel_delivered_job_withdraws_the_pr(seeded):
    await deliver_job()
    await insert_cmd("cancel")
    await work("happy.yaml")
    assert (await job()).stage == "cancelled"
    assert ctx.deps.git.calls[-1] == "withdraw"


async def test_cancel_survives_withdraw_error(seeded, monkeypatch):
    monkeypatch.setattr(worker_mod, "FakeGitService", partial(FakeGitService, fail_on="withdraw"))
    await deliver_job()
    await insert_cmd("cancel")
    await work("happy.yaml")
    assert (await job()).stage == "cancelled"


async def test_cancel_survives_keep_branch_error(seeded, monkeypatch):
    monkeypatch.setattr(
        worker_mod, "FakeGitService", partial(FakeGitService, fail_on="keep_branch")
    )
    t = start(work("slow-build.yaml"))
    await running_row("builder")
    await insert_cmd("cancel")
    await asyncio.wait_for(t, 5)
    assert ((await job()).stage, ctx.deps.sandboxes.destroyed) == ("cancelled", [(await job()).id])


async def test_cancel_queued_job(seeded):
    cid = await insert_cmd("cancel")
    await work("happy.yaml")
    assert ((await job()).stage, ctx.deps.git.calls, ctx.deps.sandboxes.destroyed) == (
        "cancelled",
        [],
        [],
    )
    assert (await cmd(cid)).result == "cancelled"


async def test_stale_epoch_does_not_consume(seeded):
    await work("escalate.yaml")
    cid = await insert_cmd("approve")
    await work("escalate.yaml", on_claim=bump_epoch)
    assert ((await cmd(cid)).consumed_at, (await job()).stage) == (None, "needs_human")


async def test_second_command_for_the_same_wait_is_superseded(seeded):
    await work("escalate.yaml")
    first = await insert_cmd("rework", note="Add a test")
    second = await insert_cmd("approve")
    await work("escalate.yaml")
    assert (
        (await cmd(first)).result,
        (await cmd(second)).result,
        (await cmd(second)).consumed_at is not None,
        (await job()).stage,
    ) == ("resumed", "superseded", True, "merged")
    assert await count_events("human.action") == 1


async def test_command_for_an_older_wait_is_stale(seeded):
    await work("escalate.yaml")
    old = await insert_cmd("approve", gate_event_id=await waiting_id() - 1)
    await work("escalate.yaml")
    assert ((await cmd(old)).result, (await job()).stage) == ("stale", "needs_human")


async def test_cli_binds_gate_commands_to_the_wait(seeded, cli):
    await work("escalate.yaml")
    assert (await cli("jobs", "rework", "demo:001", "--note", "Add a test")).exit_code == 0
    assert outcome(await cli("jobs", "approve", "demo:001")) == (
        1,
        "an action is already pending for this wait",
    )
    assert [(c.action, c.gate_event_id) for c in await commands_rows()] == [
        ("rework", await waiting_id())
    ]
    assert (await cli("jobs", "cancel", "demo:001")).exit_code == 0  # cancel is never blocked
    assert (await commands_rows())[-1].gate_event_id is None
