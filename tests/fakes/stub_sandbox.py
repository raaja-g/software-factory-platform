"""A sandbox for engine adapter tests: stub CLIs replay recorded fixtures."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tests.fakes.host_sandbox import HostSandbox

from swfactory.sandbox.base import ExecResult
from swfactory.sandbox.paths import HOME, REPO

STUBS = Path(__file__).resolve().parents[1] / "engines" / "stubs"
STUB_NAMES = ("stubcli", "claude", "opencode", "gemini", "socat")
_PREFIXES = ("/work", HOME, "/tmp")


class StubSandbox(HostSandbox):
    def __init__(
        self,
        root: Path,
        fixture: Path | None,
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> None:
        super().__init__(root)
        self.fixture = fixture
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.calls: list[dict[str, Any]] = []

    def _map(self, path: str) -> str:
        for p in _PREFIXES:
            if path == p or path.startswith(p + "/"):
                return str(self.root / path.lstrip("/"))
        return path

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str = REPO,
        env: Mapping[str, str] | None = None,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "stdin": stdin, "cwd": cwd})
        is_stub = (bool(argv) and Path(argv[0]).name in STUB_NAMES) or "gemini-run" in argv
        run_env = {
            **(env or {}),
            "PATH": f"{STUBS}{os.pathsep}{os.environ.get('PATH', '')}",
            "STUB_EXIT_CODE": str(self.exit_code if is_stub else 0),
        }
        if self.fixture is not None and is_stub:
            run_env["STUB_FIXTURE"] = str(self.fixture)
        res = await super().exec(argv, cwd=cwd, env=run_env, timeout_s=timeout_s, stdin=stdin)
        if is_stub and self.timed_out:
            res = ExecResult(res.exit_code, res.stdout, res.stderr, True, res.duration_s)
        return res
