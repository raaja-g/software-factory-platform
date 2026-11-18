"""In-memory sandbox fakes for unit tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from swfactory.sandbox.base import ExecResult, RunnerSpec, TransferTooLarge, WorkspaceOverLimit
from swfactory.sandbox.paths import REPO


class FakeSandbox:
    def __init__(
        self,
        job_id: UUID,
        exec_results: list[ExecResult] | None = None,
        marks: dict[UUID, int] | None = None,
    ) -> None:
        self.job_id = job_id
        self._marks = marks if marks is not None else {}
        self.image_digest = "sha256:fake"
        self.files: dict[str, bytes] = {}
        self.exec_results: list[ExecResult] = list(exec_results or [])
        self.exec_calls: list[list[str]] = []
        self.exec_timeouts: list[int] = []
        self.restarts = 0
        self.head = "0" * 40
        self.git_replies: dict[str, ExecResult] = {}

    def _check(self) -> None:
        limit = self._marks.get(self.job_id)
        if limit is not None:
            raise WorkspaceOverLimit(f"workspace over {limit} GB")

    def _git_reply(self, argv: Sequence[str]) -> ExecResult:
        """The scripted reply for a git argv: the subcommand, or subcommand plus last argv item."""
        rest = list(argv[1:])
        while len(rest) >= 2 and rest[0] == "-c":
            rest = rest[2:]
        if rest:
            sub = rest[0]
            best: ExecResult | None = None
            best_len = -1
            for key, reply in self.git_replies.items():
                if key == sub or (len(rest) > 1 and key == f"{sub} {rest[-1]}"):
                    if len(key) > best_len:
                        best, best_len = reply, len(key)
            if best is not None:
                return best
        return ExecResult(0, self.head + "\n", "", False, 0.0)

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str = REPO,
        env: Mapping[str, str] | None = None,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        self._check()
        self.exec_calls.append(list(argv))
        self.exec_timeouts.append(timeout_s)
        if argv and argv[0] == "git":
            return self._git_reply(argv)
        if argv and argv[0] == "find":  # the instruction-file sweep: list matching files
            root = argv[1].rstrip("/") + "/"
            names = {a for a in argv if a.endswith(".md")}
            hits = [p for p in self.files if p.startswith(root) and p.rsplit("/", 1)[-1] in names]
            return ExecResult(0, "\n".join(sorted(hits)), "", False, 0.0)
        if self.exec_results:
            return self.exec_results.pop(0)
        return ExecResult(0, "", "", False, 0.0)

    async def read_text(self, path: str) -> str | None:
        self._check()
        data = self.files.get(path)
        return None if data is None else data.decode("utf-8", errors="replace")

    async def read_bytes(self, path: str) -> bytes | None:
        self._check()
        return self.files.get(path)

    async def write_text(self, path: str, content: str) -> None:
        self._check()
        self.files[path] = content.encode()

    async def write_bytes(self, path: str, content: bytes) -> None:
        self._check()
        self.files[path] = content

    async def remove(self, path: str) -> None:
        self._check()
        self.files.pop(path, None)

    async def copy_out(self, path: str, dest: Path, *, max_bytes: int) -> int:
        self._check()
        data = self.files[path]
        if len(data) > max_bytes:
            raise TransferTooLarge(max_bytes)
        dest.write_bytes(data)
        return len(data)

    async def copy_in(self, src: Path, path: str) -> None:
        self._check()
        self.files[path] = src.read_bytes()

    async def start(self) -> None:
        self._check()
        return None

    async def stop(self) -> None:
        return None

    async def restart(self) -> None:
        self._check()
        self.restarts += 1


class FakeSandboxProvider:
    def __init__(self, exec_results: Sequence[ExecResult] = ()) -> None:
        self.exec_results = list(exec_results)
        self.boxes: dict[UUID, FakeSandbox] = {}
        self.destroyed: list[UUID] = []
        self.isolated_names: list[str] = []
        self.isolated_boxes: dict[str, FakeSandbox] = {}
        self.verify_calls: list[tuple[str, str]] = []
        self.verified: dict[UUID, FakeSandbox] = {}
        self.marks: dict[UUID, int] = {}
        self.verify_files: dict[str, bytes] = {}  # files every gate box starts with

    async def ensure(self, job_id: UUID, runner: RunnerSpec) -> FakeSandbox:
        box = self.boxes.get(job_id)
        if box is None:
            box = FakeSandbox(job_id, self.exec_results, self.marks)
            self.boxes[job_id] = box
        return box

    @asynccontextmanager
    async def isolated(
        self, job_id: UUID, runner: RunnerSpec, *, name: str
    ) -> AsyncIterator[FakeSandbox]:
        src = self.verified.get(job_id) or await self.ensure(job_id, runner)
        box = FakeSandbox(job_id, marks=self.marks)
        box.files = dict(src.files)
        box.git_replies = dict(src.git_replies)
        box.head = src.head
        self.isolated_names.append(name)
        self.isolated_boxes[name] = box
        yield box

    @asynccontextmanager
    async def verify_box(
        self, job_id: UUID, runner: RunnerSpec, *, name: str, head: str
    ) -> AsyncIterator[FakeSandbox]:
        src = await self.ensure(job_id, runner)
        box = FakeSandbox(job_id, marks=self.marks)
        box.files = {**src.files, **self.verify_files}
        box.git_replies = dict(src.git_replies)
        box.head = head
        box.exec_results = self.exec_results  # the gate boxes consume the scripted results
        self.verify_calls.append((name, head))
        self.isolated_boxes[name] = box
        self.verified[job_id] = box
        yield box

    async def destroy(self, job_id: UUID) -> None:
        self.boxes.pop(job_id, None)
        self.verified.pop(job_id, None)
        self.marks.pop(job_id, None)
        self.destroyed.append(job_id)

    async def reap_orphans(self, live: set[UUID], active: set[UUID]) -> int:
        dead = [j for j in self.boxes if j not in live and j not in active]
        for j in dead:
            del self.boxes[j]
        return len(dead)

    async def check_workspaces(self) -> list[UUID]:
        return []

    def mark_over_limit(self, job_id: UUID, max_work_gb: int) -> None:
        self.marks[job_id] = max_work_gb

    async def stop_all(self) -> int:
        return len(self.boxes)
