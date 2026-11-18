import pytest
from tests.fakes.deps import DEFAULT_TIMEOUTS
from tests.graph.test_loop import _bind, ctx, rows, run_job  # noqa: F401

from swfactory.sandbox.base import ExecResult
from swfactory.sandbox.fake import FakeSandboxProvider

pytestmark = pytest.mark.db


def er(n: int) -> ExecResult:
    return ExecResult(n, "", "", False, 0.0)


async def test_verify_fail_then_fix_attempt():
    job, st = await run_job(
        "happy.yaml", app={"test_command": "make test"}, exec_results=[er(1), er(0)]
    )
    assert (
        job.stage,
        st["round"],
        [(r.round, r.attempt) for r in await rows(job.id, "builder")],
    ) == (
        "merged",
        1,
        [(1, 1), (1, 2)],
    )


async def test_verify_fail_twice_gates():
    _, st = await run_job(
        "happy.yaml", app={"test_command": "make test"}, exec_results=[er(1), er(1)]
    )
    assert (st["gate_reason"], st["retry_node"]) == ("checks failed", "verify")


async def test_verify_writes_verify_md():
    job, _ = await run_job("happy.yaml", app={"test_command": "make test"})
    got = await ctx.deps.artifacts.get_text(job.id, "verify.md")
    assert got is not None
    assert got.splitlines()[:3] == ["# Verify: passed", "command: make test", "exit code: 0"]


async def test_verify_exec_timeout_from_definition(monkeypatch):
    seen = []
    orig = FakeSandboxProvider.ensure

    async def spy(self, job_id, runner):
        box = await orig(self, job_id, runner)
        seen.append(box)
        return box

    monkeypatch.setattr(FakeSandboxProvider, "ensure", spy)
    await run_job(
        "happy.yaml",
        app={"test_command": "make test"},
        defaults={"step_timeouts_min": {**DEFAULT_TIMEOUTS, "verify": 2}},
    )
    box = seen[0]
    assert box.exec_timeouts[box.exec_calls.index(["sh", "-c", "make test"])] == 120
