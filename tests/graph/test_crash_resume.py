"""Review focus #2: kill the worker mid-engine, mid-step and in the human gate; a restart finishes
the job with no duplicated work."""

import asyncio
import json
import os
import signal
import signal as signal_mod
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.db.repo import insert_command, latest_waiting
from swfactory.db.tables import AgentRun, Command, Job, JobEvent
from swfactory.graph.build import NODE_NAMES

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parents[1] / "fixtures" / "fake"
LEASE_S = 3
EXPECTED = {
    ("spec-writer", 1),
    ("builder", 1),
    ("reviewer-security", 1),
    ("reviewer-ux", 1),
    ("reviewer-ui", 1),
    ("reviewer-code", 1),
    ("approver", 1),
}


class Ctx:
    sessions: Any = None
    url = ""
    git_state: Path


ctx = Ctx()


@pytest.fixture(autouse=True)
def _bind(sessions, pg_url, tmp_path):
    ctx.sessions, ctx.url, ctx.git_state = sessions, pg_url, tmp_path / "git.json"


def spawn(script, pause=None, lease_s=LEASE_S):
    env = {k: v for k, v in os.environ.items() if k != "FACTORY_DATABASE_PASSWORD_FILE"}
    env["FACTORY_DATABASE_URL"] = ctx.url
    env.pop("FACTORY_TEST_PAUSE", None)
    env.pop("FACTORY_TEST_HOOKS", None)
    if pause:
        env.update(FACTORY_TEST_HOOKS="1", FACTORY_TEST_PAUSE=pause)
    cmd = [
        sys.executable,
        "-c",
        "from swfactory.cli.main import app; app()",
        "worker",
        "--fake-script",
        str(FIXTURES / script),
        "--fake-git-state",
        str(ctx.git_state),
        "--lease-s",
        str(lease_s),
        "--renew-s",
        "1",
        "--exit-when-idle",
    ]
    return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def until(cond, timeout, what):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def job():
    async with ctx.sessions() as s:
        return await s.scalar(select(Job).execution_options(populate_existing=True))


async def agent_row(role, rnd, status=None):
    async with ctx.sessions() as s:
        q = select(AgentRun).where(AgentRun.role == role, AgentRun.round == rnd)
        if status:
            q = q.where(AgentRun.status == status)
        return (await s.scalars(q.order_by(AgentRun.attempt).limit(1))).first()


async def ok_counts_by_role_round():
    async with ctx.sessions() as s:
        rows = await s.execute(
            text("SELECT role, round, count(*) FROM app.agent_runs WHERE status='ok' GROUP BY 1,2")
        )
        return {(r[0], r[1]): r[2] for r in rows}


async def dup_finished():
    async with ctx.sessions() as s:
        rows = await s.execute(
            text(
                "SELECT data->>'run_id' FROM app.job_events WHERE kind='run.finished' "
                "GROUP BY 1 HAVING count(*) > 1"
            )
        )
        return list(rows)


async def count_events(kind):
    async with ctx.sessions() as s:
        return await s.scalar(select(func.count()).where(JobEvent.kind == kind))


def fake_git_state():
    return json.loads(ctx.git_state.read_text())


async def approve_command():
    async with ctx.sessions() as s:
        return await s.scalar(select(Command).execution_options(populate_existing=True))


async def assert_no_duplicates():
    counts = await ok_counts_by_role_round()
    assert all(n == 1 for n in counts.values())
    assert set(counts) == EXPECTED
    assert await dup_finished() == []
    assert len(fake_git_state()["delivered"]) == 1
    assert await count_events("human.action") <= 1


EXIT: dict[str, int | None] = {"code": None}


def last_exit_code():
    return EXIT["code"]


async def crash_and_resume(
    script,
    *,
    pause=None,
    kill_when=None,
    app=None,
    approve_first=False,
    signal=signal.SIGKILL,  # noqa: A002
    lease_s=LEASE_S,
):
    await seed_demo(ctx.sessions, defaults={"engine_retry_backoff_s": [0, 0]}, app=app or {})
    await enq(ctx.sessions, app="demo")
    if approve_first:
        p = spawn(script)
        await asyncio.to_thread(p.wait, 60)

        async def waiting():
            return (await job()).stage == "needs_human"

        await until(waiting, 10, "needs_human")
        async with ctx.sessions() as s:
            wait = await latest_waiting(s, (await job()).id)
        assert wait is not None
        async with ctx.sessions.begin() as s:
            await insert_command(
                s,
                job_id=(await job()).id,
                action="approve",
                note=None,
                requested_by="ann",
                gate_event_id=wait[0],
            )
    proc = spawn(script, pause=pause, lease_s=lease_s)
    try:
        if kill_when is not None:

            async def running():
                return await agent_row(*kill_when, status="running") is not None

            await until(running, 30, "a running agent_runs row")
        else:
            marker = Path(pause.split(":", 2)[2])  # type: ignore[union-attr]

            async def marked():
                return marker.exists()

            await until(marked, 30, "the pause marker")
    finally:
        proc.send_signal(signal)
        try:
            EXIT["code"] = proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            EXIT["code"] = None
    killed = await agent_row(*kill_when) if kill_when is not None else None
    if signal == signal_mod.SIGKILL:
        await asyncio.sleep(lease_s + 1)  # the dead worker's lease runs out

    async def merged():
        nonlocal proc2
        if proc2.poll() is not None and (await job()).stage != "merged":
            proc2 = spawn(
                script, lease_s=lease_s
            )  # exited idle before the lease ran out: try again
        j = await job()
        # A graph node writes merged; the worker then consumes the command and releases its lease.
        # Wait for no live lease (a dead worker's expired one can stay on a finished job), or the
        # test would kill the worker half way through its last step.
        live = j.lease_until is not None and j.lease_until > datetime.now(UTC)
        return j.stage == "merged" and not live

    proc2 = spawn(script, lease_s=lease_s)
    try:
        await until(merged, 60, "stage merged")
    finally:
        proc2.kill()
        proc2.wait()
    return killed


@pytest.mark.parametrize(
    "script,kill_when",
    [("slow-build.yaml", ("builder", 1)), ("slow-review.yaml", ("reviewer-security", 1))],
)
async def test_kill_mid_engine_then_resume(script, kill_when):
    await crash_and_resume(script, kill_when=kill_when)
    async with ctx.sessions() as s:
        runs = (
            await s.scalars(
                select(AgentRun).where(AgentRun.role == kill_when[0], AgentRun.status == "failed")
            )
        ).all()
    assert [(r.status, r.error) for r in runs] == [("failed", "interrupted")]
    await assert_no_duplicates()


@pytest.mark.parametrize("node", [n for n in NODE_NAMES if n != "human_gate"])
async def test_kill_mid_step_then_resume(node, tmp_path):
    await crash_and_resume(
        "happy.yaml",
        pause=f"{node}:30:{tmp_path / 'marker'}",
        app={"test_command": "make test"},
    )
    assert (await job()).stage == "merged"
    await assert_no_duplicates()


async def test_kill_in_human_gate_after_resume(tmp_path):
    await crash_and_resume(
        "escalate.yaml", pause=f"human_gate:30:{tmp_path / 'marker'}", approve_first=True
    )
    c = await approve_command()
    assert (c.consumed_at is not None, c.result, await count_events("human.action")) == (
        True,
        "resumed",
        1,
    )


async def test_sigterm_releases_the_lease_at_once():  # lease 90 s: only a released lease finishes
    killed_row = await crash_and_resume(
        "slow-build.yaml", kill_when=("builder", 1), signal=signal.SIGTERM, lease_s=90
    )
    assert (
        killed_row is not None and killed_row.status == "running"
    )  # the stopped worker left it; the resume cleans it up
    row = await agent_row("builder", 1)  # attempt 1, re-read after the resume
    assert row is not None
    assert (row.status, row.error, last_exit_code()) == ("failed", "interrupted", 0)
    await assert_no_duplicates()
