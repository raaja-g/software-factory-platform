from uuid import uuid4

from swfactory.sandbox.base import ExecResult, RunnerSpec
from swfactory.sandbox.fake import FakeSandboxProvider
from swfactory.sandbox.paths import IN

J, A, B = uuid4(), uuid4(), uuid4()
RUNNER = RunnerSpec("standard", "img", 2, 4096, 512, ())


def er(n: int) -> ExecResult:
    return ExecResult(n, "", "", False, 0.0)


async def test_fake_sandbox_files_and_isolated_copy():
    p = FakeSandboxProvider()
    box = await p.ensure(J, RUNNER)
    await box.write_text(f"{IN}/a.md", "x")
    assert (await box.read_text(f"{IN}/a.md"), await box.read_text(f"{IN}/missing.md")) == (
        "x",
        None,
    )
    async with p.isolated(J, RUNNER, name="verify") as iso:
        assert await iso.read_text(f"{IN}/a.md") == "x"
        await iso.write_text(f"{IN}/a.md", "y")
        assert iso is not box
    assert await box.read_text(f"{IN}/a.md") == "x"


async def test_fake_sandbox_exec_results_in_order_then_exit_0():
    box = await FakeSandboxProvider(exec_results=[er(1), er(0)]).ensure(J, RUNNER)
    assert [(await box.exec(["true"], timeout_s=5)).exit_code for _ in range(4)] == [1, 0, 0, 0]
    assert (box.exec_calls, box.exec_timeouts) == ([["true"]] * 4, [5] * 4)


async def test_fake_sandbox_git_does_not_consume_exec_results():
    box = await FakeSandboxProvider(exec_results=[er(1)]).ensure(J, RUNNER)
    assert (await box.exec(["git", "rev-parse", "HEAD"])).stdout == "0" * 40 + "\n"
    assert (await box.exec(["sh", "-c", "make test"])).exit_code == 1
    box.head = "a" * 40
    assert (await box.exec(["git", "rev-parse", "HEAD"])).stdout == "a" * 40 + "\n"


async def test_fake_sandbox_restart_counts():
    box = await FakeSandboxProvider().ensure(J, RUNNER)
    await box.write_text(f"{IN}/p.md", "kept")
    await box.restart()
    assert (box.restarts, await box.read_text(f"{IN}/p.md")) == (1, "kept")


async def test_provider_ensure_destroy_reap():
    p = FakeSandboxProvider()
    b1 = await p.ensure(A, RUNNER)
    assert await p.ensure(A, RUNNER) is b1
    await p.destroy(A)
    assert p.destroyed == [A] and await p.ensure(A, RUNNER) is not b1
    await p.ensure(B, RUNNER)
    assert await p.reap_orphans(live={A}, active=set()) == 1
