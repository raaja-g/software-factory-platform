"""A test sandbox that runs commands on the host, with /work and /home/factory under a root."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from swfactory.sandbox.base import ExecResult, TransferTooLarge
from swfactory.sandbox.paths import HOME, REPO

_PREFIXES = ("/work", HOME)


class HostSandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.job_id = uuid.uuid4()
        self.image_digest = "sha256:host"
        (root / "work").mkdir(parents=True, exist_ok=True)
        (root / HOME.lstrip("/")).mkdir(parents=True, exist_ok=True)

    def _map(self, path: str) -> str:
        for p in _PREFIXES:
            if path == p or path.startswith(p + "/"):
                return str(self.root / path.lstrip("/"))
        return path

    def _host(self, path: str) -> Path:
        return Path(self._map(path))

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str = REPO,
        env: Mapping[str, str] | None = None,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        workdir = self._host(cwd)
        workdir.mkdir(parents=True, exist_ok=True)
        full_env = {**os.environ, **(env or {})}
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *(self._map(a) for a in argv),
            cwd=workdir,
            env=full_env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else None), timeout_s
            )
            timed_out = False
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timed_out = True
        return ExecResult(
            proc.returncode if proc.returncode is not None else -1,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
            timed_out,
            time.monotonic() - start,
        )

    async def read_bytes(self, path: str) -> bytes | None:
        p = self._host(path)
        return p.read_bytes() if p.is_file() else None

    async def read_text(self, path: str) -> str | None:
        data = await self.read_bytes(path)
        return None if data is None else data.decode("utf-8", errors="replace")

    async def write_bytes(self, path: str, content: bytes) -> None:
        p = self._host(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    async def write_text(self, path: str, content: str) -> None:
        await self.write_bytes(path, content.encode())

    async def remove(self, path: str) -> None:
        self._host(path).unlink(missing_ok=True)

    async def copy_out(self, path: str, dest: Path, *, max_bytes: int) -> int:
        src = self._host(path)
        size = src.stat().st_size
        if size > max_bytes:
            raise TransferTooLarge(max_bytes)
        shutil.copyfile(src, dest)
        return size

    async def copy_in(self, src: Path, path: str) -> None:
        p = self._host(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, p)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def restart(self) -> None:
        return None
