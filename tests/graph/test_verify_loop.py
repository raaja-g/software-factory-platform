import pytest
from tests.fakes.deps import DEFAULT_TIMEOUTS
from tests.graph.test_loop import _bind, ctx, rows, run_job  # noqa: F401

from swfactory.sandbox.base import ExecResult

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
    assert (st["gate_reason"], st["retry_node"]) == (
        "checks failed: tests failed (exit 1)",
        "verify",
    )


def gate_box(n: int):
    return ctx.deps.sandboxes.isolated_boxes[f"gate-r1-a{n}"]


async def test_verify_writes_verify_md():
    job, _ = await run_job("happy.yaml", app={"test_command": "make test"})
    got = await ctx.deps.artifacts.get_text(job.id, "verify.md")
    assert got is not None and got.startswith("# Verify") and "Status: PASSED" in got
    assert ["sh", "-c", "make test"] not in gate_box(1).exec_calls


async def test_verify_exec_timeout_from_definition():
    await run_job(
        "happy.yaml",
        app={"test_command": "make test"},
        defaults={"step_timeouts_min": {**DEFAULT_TIMEOUTS, "verify": 45}},
    )
    box = gate_box(1)
    assert box.exec_timeouts[box.exec_calls.index(["bash", "-lc", "make test"])] == 2700
