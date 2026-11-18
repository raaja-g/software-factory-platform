"""In-memory sandbox fakes for unit tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from swfactory.sandbox.base import ExecResult, RunnerSpec
from swfactory.sandbox.paths import REPO


class FakeSandbox:
    def __init__(self, job_id: UUID, exec_results: list[ExecResult] | None = None) -> None:
        self.job_id = job_id
        self.image_digest = "sha256:fake"
        self.files: dict[str, bytes] = {}
        self.exec_results: list[ExecResult] = list(exec_results or [])
        self.exec_calls: list[list[str]] = []
        self.exec_timeouts: list[int] = []
        self.restarts = 0
        self.head = "0" * 40

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str = REPO,
        env: Mapping[str, str] | None = None,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        self.exec_calls.append(list(argv))
        self.exec_timeouts.append(timeout_s)
        if argv and argv[0] == "git":
            return ExecResult(0, self.head + "\n", "", False, 0.0)
        if self.exec_results:
            return self.exec_results.pop(0)
        return ExecResult(0, "", "", False, 0.0)

    async def read_text(self, path: str) -> str | None:
        data = self.files.get(path)
        return None if data is None else data.decode("utf-8", errors="replace")

    async def read_bytes(self, path: str) -> bytes | None:
        return self.files.get(path)

    async def write_text(self, path: str, content: str) -> None:
        self.files[path] = content.encode()

    async def write_bytes(self, path: str, content: bytes) -> None:
        self.files[path] = content

    async def copy_out(self, path: str, dest: Path, *, max_bytes: int) -> int:
        data = self.files[path]
        if len(data) > max_bytes:
            raise ValueError(f"{path} is larger than {max_bytes} bytes")
        dest.write_bytes(data)
        return len(data)

    async def copy_in(self, src: Path, path: str) -> None:
        self.files[path] = src.read_bytes()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def restart(self) -> None:
        self.restarts += 1


class FakeSandboxProvider:
    def __init__(self, exec_results: Sequence[ExecResult] = ()) -> None:
        self.exec_results = list(exec_results)
        self.boxes: dict[UUID, FakeSandbox] = {}
        self.destroyed: list[UUID] = []

    async def ensure(self, job_id: UUID, runner: RunnerSpec) -> FakeSandbox:
        box = self.boxes.get(job_id)
        if box is None:
            box = FakeSandbox(job_id, self.exec_results)
            self.boxes[job_id] = box
        return box

    @asynccontextmanager
    async def isolated(
        self, job_id: UUID, runner: RunnerSpec, *, name: str
    ) -> AsyncIterator[FakeSandbox]:
        src = await self.ensure(job_id, runner)
        box = FakeSandbox(job_id)
        box.files = dict(src.files)
        yield box

    async def destroy(self, job_id: UUID) -> None:
        self.boxes.pop(job_id, None)
        self.destroyed.append(job_id)

    async def reap_orphans(self, live: set[UUID], active: set[UUID]) -> int:
        dead = [j for j in self.boxes if j not in live and j not in active]
        for j in dead:
            del self.boxes[j]
        return len(dead)

    async def check_workspaces(self) -> list[UUID]:
        return []

    async def stop_all(self) -> int:
        return len(self.boxes)
