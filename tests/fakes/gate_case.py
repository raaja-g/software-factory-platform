"""A scripted VerifyGate run for the gate tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from tests.fakes.artifacts import InMemoryArtifactStore

from swfactory.definition.models import SmokeConfig
from swfactory.gitops.service import DiffStat, GitService
from swfactory.sandbox.base import ExecResult, RunnerSpec, Sandbox
from swfactory.sandbox.fake import FakeSandbox, FakeSandboxProvider
from swfactory.sandbox.paths import OUT
from swfactory.verify.gate import GateResult, VerifyGate

HEAD = "a" * 40
BASE = "b" * 40
BRANCH = "factory/001-x"
RUNNER = RunnerSpec("standard", "img", 2, 4096, 512, ())
DEFAULT_PATCH = "diff --git a/a.py b/a.py\n+x\n"
DEFAULT_AGENTS = "Test command: `pytest -q`"


def _er(code: int, out: str = "", timed_out: bool = False) -> ExecResult:
    return ExecResult(code, out, "", timed_out, 0.0)


class StubGit(GitService):
    def __init__(self, rebase: bool, diff_paths: Sequence[str]) -> None:
        self._rebase = rebase
        self.diff_paths = tuple(diff_paths)
        self.diff_stat_boxes: list[Sandbox] = []

    async def rebase(self, job: Any, app: Any, box: Sandbox) -> bool:
        return self._rebase

    async def base_head(self, job: Any, app: Any) -> str:
        return BASE

    async def diff_stat(self, box: Sandbox, base: str) -> DiffStat:
        self.diff_stat_boxes.append(box)
        return DiffStat(len(self.diff_paths), 0, 0, self.diff_paths)


class _Provider(FakeSandboxProvider):
    def __init__(self, verify_results: list[ExecResult]) -> None:
        super().__init__()
        self.verify_results = verify_results

    @asynccontextmanager
    async def verify_box(
        self, job_id: Any, runner: RunnerSpec, *, name: str, head: str
    ) -> AsyncIterator[FakeSandbox]:
        async with super().verify_box(job_id, runner, name=name, head=head) as box:
            box.exec_results = list(self.verify_results)
            yield box


@dataclass
class GateCase:
    r: GateResult
    provider: _Provider
    job_box: FakeSandbox
    iso: FakeSandbox
    artifacts: dict[str, str]
    kinds: dict[str, str]
    git: StubGit


def git_subcommands(box: FakeSandbox) -> set[str]:
    out: set[str] = set()
    for argv in box.exec_calls:
        if not argv or argv[0] != "git":
            continue
        rest = argv[1:]
        while len(rest) >= 2 and rest[0] == "-c":
            rest = rest[2:]
        if rest:
            out.add(rest[0])
    return out


async def run_gate(**kw: Any) -> GateCase:
    attempt = kw.get("attempt", 1)
    setup: Sequence[str] = kw.get("setup", ())
    results = [_er(kw.get("setup_exit", 0)) for _ in setup]
    results.append(
        _er(kw.get("test_exit", 0), kw.get("test_output", ""), kw.get("timed_out", False))
    )
    provider = _Provider(results)
    job = SimpleNamespace(id=uuid4(), seq=1, slug="x", branch=BRANCH)
    smoke = kw.get("smoke")
    app = SimpleNamespace(
        protected_paths=tuple(kw.get("protected", ())),
        test_command=None,
        default_branch="main",
        runner="web" if smoke is not None else "standard",
        smoke=SmokeConfig(
            start_command="python3 app.py", port=8000, paths=["/"], screenshots=["home"]
        )
        if smoke is not None
        else None,
    )
    box = await provider.ensure(job.id, RUNNER)
    box.head = HEAD
    replies = box.git_replies
    replies["symbolic-ref"] = _er(0, kw.get("symref", f"refs/heads/{BRANCH}") + "\n")
    replies["merge-base"] = _er(kw.get("on_base", 0))
    for key, name in (
        (f"show {BASE}:AGENTS.md", "base_agents"),
        ("show HEAD:AGENTS.md", "head_agents"),
    ):
        text = kw.get(name, DEFAULT_AGENTS)
        replies[key] = _er(128) if text is None else _er(0, text)
    box.files[f"{OUT}/diff.patch"] = kw.get("patch", DEFAULT_PATCH).encode()
    git = StubGit(kw.get("rebase", True), kw.get("diff_paths", ()))
    store = InMemoryArtifactStore()
    gate = VerifyGate(git, store, provider, smoke)  # type: ignore[arg-type]
    r = await gate.run(
        job,
        app,
        box,
        runner=RUNNER,
        round=1,
        attempt=attempt,
        setup=setup,
        test_timeout_s=kw.get("test_timeout_s", 900),
    )
    iso = provider.isolated_boxes.get(f"gate-r1-a{attempt}", box)
    artifacts = {p: b for (_, p), (_, b) in store._rows.items()}
    kinds = {p: m.kind for (_, p), (m, _) in store._rows.items()}
    return GateCase(r, provider, box, iso, artifacts, kinds, git)
