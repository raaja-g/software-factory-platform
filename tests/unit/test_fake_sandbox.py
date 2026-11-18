from uuid import uuid4

import pytest

from swfactory.sandbox.base import ExecResult, RunnerSpec, TransferTooLarge, running
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


async def test_fake_isolated_is_separate():
    provider = FakeSandboxProvider()
    async with provider.isolated(J, RUNNER, name="review-code-r1") as iso:
        await iso.write_text("/work/x", "1")
    assert provider.isolated_names == ["review-code-r1"]
    assert await (await provider.ensure(J, RUNNER)).read_text("/work/x") is None
    assert provider.isolated_boxes["review-code-r1"] is iso


async def test_fake_verify_box_feeds_isolated():
    provider = FakeSandboxProvider()
    async with provider.verify_box(J, RUNNER, name="gate-r1-a1", head="a" * 40) as vb:
        await vb.write_text("/work/.factory/in/screenshots/home.png", "png")
    async with provider.isolated(J, RUNNER, name="review-code-r1") as iso:
        assert await iso.read_text("/work/.factory/in/screenshots/home.png") == "png"
    assert provider.verify_calls == [("gate-r1-a1", "a" * 40)]
    assert provider.isolated_boxes["gate-r1-a1"] is vb


async def test_fake_copy_in_and_out(tmp_path):
    box = await FakeSandboxProvider().ensure(J, RUNNER)
    (tmp_path / "a").write_bytes(b"x" * 10)
    await box.copy_in(tmp_path / "a", "/work/.factory/git/a.bundle")
    assert await box.copy_out("/work/.factory/git/a.bundle", tmp_path / "b", max_bytes=10) == 10
    with pytest.raises(TransferTooLarge):
        await box.copy_out("/work/.factory/git/a.bundle", tmp_path / "c", max_bytes=9)


async def test_running_starts_and_stops():
    box = await FakeSandboxProvider().ensure(J, RUNNER)
    calls: list[str] = []
    box.start = lambda: _rec(calls, "start")  # type: ignore[method-assign]
    box.stop = lambda: _rec(calls, "stop")  # type: ignore[method-assign]
    async with running(box) as b:
        assert b is box and calls == ["start"]
    assert calls == ["start", "stop"]


async def _rec(calls: list[str], what: str) -> None:
    calls.append(what)


async def test_fake_git_replies_and_prepared_branch():
    from types import SimpleNamespace

    from swfactory.gitops.fake import FakeGitService
    from swfactory.queue import branch_name

    job = SimpleNamespace(id=uuid4(), seq=7, slug="fix-it")
    p = FakeSandboxProvider()
    box = await p.ensure(job.id, RUNNER)
    box.git_replies["show b:AGENTS.md"] = er(128)
    await FakeGitService().prepare(job, SimpleNamespace(), box)  # type: ignore[arg-type]
    r = await box.exec(["git", "-c", "core.pager=cat", "symbolic-ref", "HEAD"])
    assert r.stdout == f"refs/heads/{branch_name(job.seq, job.slug)}\n"
    assert (await box.exec(["git", "show", "b:AGENTS.md"])).exit_code == 128
    assert (await box.exec(["git", "show", "x"])).stdout == box.head + "\n"
    async with p.verify_box(job.id, RUNNER, name="gate-r1-a1", head=box.head) as vb:
        assert vb.git_replies == box.git_replies
