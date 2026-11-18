"""Sandbox protocols shared by the Docker provider and the fakes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from swfactory.sandbox.paths import REPO


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


READ_BYTES_MAX = 512 * 2**20


class SandboxFileTooLarge(Exception): ...


class WorkspaceOverLimit(Exception): ...


@dataclass(frozen=True, slots=True)
class RunnerSpec:
    name: str
    image: str
    cpus: float
    memory_mb: int
    pids: int
    allowed_hosts: tuple[str, ...]
    egress_port: int = 3128
    max_work_gb: int = 20


class Sandbox(Protocol):
    job_id: UUID
    image_digest: str

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str = REPO,
        env: Mapping[str, str] | None = None,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult: ...
    async def read_text(self, path: str) -> str | None: ...
    async def read_bytes(self, path: str) -> bytes | None: ...
    async def write_text(self, path: str, content: str) -> None: ...
    async def write_bytes(self, path: str, content: bytes) -> None: ...
    async def copy_out(self, path: str, dest: Path, *, max_bytes: int) -> int: ...
    async def copy_in(self, src: Path, path: str) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def restart(self) -> None: ...


class SandboxProvider(Protocol):
    async def ensure(self, job_id: UUID, runner: RunnerSpec) -> Sandbox: ...
    def verify_box(
        self, job_id: UUID, runner: RunnerSpec, *, name: str, head: str
    ) -> AbstractAsyncContextManager[Sandbox]: ...
    def isolated(
        self, job_id: UUID, runner: RunnerSpec, *, name: str
    ) -> AbstractAsyncContextManager[Sandbox]: ...
    async def destroy(self, job_id: UUID) -> None: ...
    async def reap_orphans(self, live: set[UUID], active: set[UUID]) -> int: ...
    async def check_workspaces(self) -> list[UUID]: ...
    async def stop_all(self) -> int: ...
