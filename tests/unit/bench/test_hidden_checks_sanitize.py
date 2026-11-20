"""run_hidden_checks sanitizes the checks box first, passes the task's check files as `keep`,
records what it removed, and never fails the trial just because something was removed."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from tests.fakes.artifacts import InMemoryArtifactStore

from swfactory.bench import runner
from swfactory.bench.runner import HIDDEN_CHECKS_BOX, HIDDEN_LOG, run_hidden_checks
from swfactory.definition.models import BenchTaskDef
from swfactory.sandbox.base import ExecResult, RunnerSpec
from swfactory.sandbox.fake import FakeSandbox, FakeSandboxProvider

SHA = "a" * 40
RUNNER = RunnerSpec("standard", "img", 2, 4096, 512, ())
TASK = BenchTaskDef(
    title="t",
    feature="f",
    repo="local:demo",
    base_sha=SHA,
    merge_sha=SHA,
    definition_sha=SHA,
    check_command="python3 -m unittest -q {file}",
    check_files={"tests/test_b.py": "B = 1\n", "tests/test_a.py": "A = 1\n"},
)


class Stored:
    """A stand-in session factory that records the hidden_* columns of the result update."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    @asynccontextmanager
    async def begin(self):
        outer = self

        class S:
            async def execute(self, stmt):
                outer.values.update(stmt.compile().params)

        yield S()


def make_deps():
    return SimpleNamespace(
        sandboxes=FakeSandboxProvider(), artifacts=InMemoryArtifactStore(), sessions=Stored()
    )


def spy(monkeypatch, *, returns=(), raises=None):
    seen: list[dict] = []

    async def fake_sanitize(box: FakeSandbox, base_sha, keep):
        seen.append(
            {
                "box": box,
                "base": base_sha,
                "keep": set(keep),
                "files_then": sorted(box.files),
                "execs_then": len(box.exec_calls),
            }
        )
        if raises is not None:
            raise raises
        return list(returns)

    monkeypatch.setattr(runner, "sanitize_tree", fake_sanitize)
    return seen


async def run(deps, **kw):
    return await run_hidden_checks(deps, uuid4(), TASK, runner=RUNNER, fresh=True, **kw)


async def test_sanitizer_runs_before_the_check_files_are_written(monkeypatch):
    seen = spy(monkeypatch)
    deps = make_deps()
    res = await run(deps)
    assert len(seen) == 1
    only = seen[0]
    assert only["box"] is deps.sandboxes.isolated_boxes[HIDDEN_CHECKS_BOX]
    assert only["base"] == SHA
    assert only["files_then"] == []  # no check file was in the box yet ...
    assert only["execs_then"] == 0  # ... and no check had run
    assert only["box"].files["/work/repo/tests/test_a.py"] == b"A = 1\n"  # ... and then they were
    assert res.passed is True and res.sanitized == ()


async def test_keep_is_the_check_files_keys(monkeypatch):
    seen = spy(monkeypatch)
    await run(make_deps(), also_base=True)
    assert seen[0]["keep"] == {"tests/test_a.py", "tests/test_b.py"}


async def test_removed_paths_are_reported_in_result_detail_and_log(monkeypatch):
    spy(monkeypatch, returns=["conftest.py", "tests/conftest.py"])
    deps = make_deps()
    res = await run(deps)
    assert res.sanitized == ("conftest.py", "tests/conftest.py")
    assert res.passed is True  # removing files does not fail the trial
    detail = json.loads(str(deps.sessions.values["hidden_detail"]))
    assert detail["sanitized"] == ["conftest.py", "tests/conftest.py"]
    assert detail["files"] == {"tests/test_a.py": 0, "tests/test_b.py": 0}
    log = await deps.artifacts.get_text(next(iter(deps.sandboxes.boxes)), HIDDEN_LOG)
    assert log is not None
    lines = log.splitlines()
    assert lines[:2] == ["sanitized: conftest.py", "sanitized: tests/conftest.py"]
    assert lines.index("sanitized: conftest.py") < next(
        i for i, ln in enumerate(lines) if "$" in ln
    )


async def test_detail_is_unchanged_when_nothing_was_sanitized(monkeypatch):
    spy(monkeypatch)
    deps = make_deps()
    await run(deps)
    detail = json.loads(str(deps.sessions.values["hidden_detail"]))
    assert detail == {"files": {"tests/test_a.py": 0, "tests/test_b.py": 0}}


async def test_checks_still_decide_the_result_after_sanitizing(monkeypatch):
    spy(monkeypatch, returns=["conftest.py"])
    deps = make_deps()
    deps.sandboxes.exec_results = [
        ExecResult(1, "", "", False, 0.1),
        ExecResult(0, "", "", False, 0.1),
    ]
    res = await run(deps)
    assert res.passed is False and res.sanitized == ("conftest.py",)


async def test_sanitizer_failure_means_the_checks_do_not_run(monkeypatch):
    spy(monkeypatch, raises=RuntimeError("cannot list the tree"))
    deps = make_deps()
    res = await run(deps)
    box = deps.sandboxes.isolated_boxes[HIDDEN_CHECKS_BOX]
    assert res.passed is None and res.error == "hidden checks could not run: cannot list the tree"
    assert box.exec_calls == [] and not any(p.startswith("/work/repo/tests") for p in box.files)


async def test_base_pass_rewrites_the_check_files_and_keeps_the_sanitized_list(monkeypatch):
    spy(monkeypatch, returns=["pytest.py"])
    deps = make_deps()
    res = await run(deps, also_base=True)
    box = deps.sandboxes.isolated_boxes[HIDDEN_CHECKS_BOX]
    assert res.sanitized == ("pytest.py",) and set(res.base_files) == set(TASK.check_files)
    assert box.files["/work/repo/tests/test_b.py"] == b"B = 1\n"


async def test_check_command_runs_under_plain_bash_not_a_login_shell(monkeypatch):
    spy(monkeypatch)
    deps = make_deps()
    await run(deps)
    box = deps.sandboxes.isolated_boxes[HIDDEN_CHECKS_BOX]
    assert box.exec_calls == [
        ["bash", "-c", "python3 -m unittest -q tests/test_a.py"],
        ["bash", "-c", "python3 -m unittest -q tests/test_b.py"],
    ]


async def test_real_sanitizer_is_wired_in_and_does_not_disturb_the_fake_box():
    """Without the spy: the real sanitize_tree on a FakeSandbox sees no candidates."""
    deps = make_deps()
    res = await run(deps)
    box = deps.sandboxes.isolated_boxes[HIDDEN_CHECKS_BOX]
    assert res.passed is True and res.sanitized == ()
    assert box.exec_calls[0][0] == "find"
    assert [c for c in box.exec_calls if c[0] == "bash"] == [
        ["bash", "-c", "python3 -m unittest -q tests/test_a.py"],
        ["bash", "-c", "python3 -m unittest -q tests/test_b.py"],
    ]
