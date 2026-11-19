"""Fakes for GitHubGit: host-run gitops, a token-recording auth, a crashing FakeGitHub."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from tests.fakes.github import FakeGitHub

from swfactory.github.client import GitHubError, PullRef
from swfactory.gitops.github import GITOPS_REPO, GITOPS_XFER
from swfactory.sandbox.base import ExecResult, TransferTooLarge

REMOTE_URL = "https://github.com/acme/web.git"


class SimulatedCrash(Exception):
    pass


class FakeAuth:
    """Mints numbered tokens and remembers which permissions each one carries."""

    def __init__(self) -> None:
        self.perms: dict[str, dict[str, str]] = {}
        self.minted: list[dict[str, str]] = []

    async def token_for(self, repo: str, permissions: Mapping[str, str]) -> str:
        tok = f"tok-{len(self.perms) + 1}"
        self.perms[tok] = dict(permissions)
        self.minted.append(dict(permissions))
        return tok

    async def org_token(self, org: str, permissions: Mapping[str, str]) -> str:
        return await self.token_for(org, permissions)


class DeliveryGitHub(FakeGitHub):
    """FakeGitHub plus the hooks the delivery tests need."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.pushes = 0
        self.push_leases: list[str] = []
        self.merged_times = 0
        self.merge_calls: list[tuple[int, str, str]] = []
        self.merge_blocked = False
        self.defer_merge = False
        self.status_override: str | None = None
        self._crash: str | None = None

    @property
    def counts(self) -> Counter[str]:
        out = Counter(self.calls)
        out["push"] = self.pushes
        return out

    def crash_once_after(self, name: str) -> None:
        self._crash = name

    def maybe_crash(self, name: str) -> None:
        if self._crash == name:
            self._crash = None
            raise SimulatedCrash(name)

    def pr_for(self, branch: str) -> dict[str, Any]:
        return next(d for d in self.prs.values() if d["head"] == branch)

    def labels_of(self, branch: str) -> set[str]:
        n = next(n for n, d in self.prs.items() if d["head"] == branch)
        return self.labels.get(n, set())

    def number_of(self, branch: str) -> int:
        return next(n for n, d in self.prs.items() if d["head"] == branch)

    async def create_pr(self, **kw: Any) -> PullRef:
        pr = await super().create_pr(**kw)
        self.maybe_crash("create_pr")
        return pr

    async def comment_once(self, number: int, body: str, *, marker: str) -> bool:
        out = await super().comment_once(number, body, marker=marker)
        self.maybe_crash("comment_once")
        return out

    async def upsert_check_run(self, **kw: Any) -> int:
        out = await super().upsert_check_run(**kw)
        self.maybe_crash("upsert_check_run")
        return out

    async def merge_async_status(self, number: int, uuid: str) -> str:
        out = await super().merge_async_status(number, uuid)
        self.maybe_crash("merge_async_status")
        return self.status_override or out

    async def merge_async(self, number: int, *, sha: str, method: str) -> str:
        if self.merge_blocked:
            self._count("merge_async")
            raise GitHubError(405, "Pull request is blocked by branch protection")
        if self.defer_merge:
            self._count("merge_async")
            uuid = f"u-{len(self.merges) + 1}"
            self.merges[uuid] = (number, sha, method)
        else:
            uuid = await super().merge_async(number, sha=sha, method=method)
        self.merged_times += 1
        self.merge_calls.append((number, sha, method))
        self.maybe_crash("merge_async")
        return uuid


def _clean(argv: Sequence[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for a in argv:
        if skip:
            skip = False
        elif a == "-c":
            skip = True
        else:
            out.append(a)
    return out


class FakeGitops:
    """Runs the gitops argv with the host's git on a per-job directory; GitHub is a local bare."""

    def __init__(self, root: Path, origin: Path, gh: DeliveryGitHub, auth: FakeAuth) -> None:
        self.root = root
        self.origin = origin
        self.gh = gh
        self.auth = auth
        self.handed: list[dict[str, str]] = []
        self.argvs: list[list[str]] = []

    def _dir(self, job_id: UUID) -> Path:
        d = self.root / str(job_id)
        (d / "xfer").mkdir(parents=True, exist_ok=True)
        return d

    def _map(self, job_id: UUID, value: str) -> str:
        d = self._dir(job_id)
        return (
            value.replace(GITOPS_REPO, str(d / "repo.git"))
            .replace(GITOPS_XFER, str(d / "xfer"))
            .replace(REMOTE_URL, str(self.origin))
        )

    def _git(self, argv: list[str]) -> ExecResult:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        p = subprocess.run(argv, capture_output=True, text=True, env=env)
        return ExecResult(p.returncode, p.stdout, p.stderr, False, 0.0)

    def _sync_main(self) -> None:
        r = subprocess.run(
            ["git", "-C", str(self.origin), "rev-parse", "refs/heads/main"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            self.gh.branches["main"] = r.stdout.strip()

    async def run(
        self, job_id: UUID, argv: Sequence[str], *, token: str | None, timeout_s: int | None = None
    ) -> ExecResult:
        self.argvs.append(list(argv))
        if token is not None:
            self.handed.append(self.auth.perms[token])
        args = [self._map(job_id, a) for a in _clean(argv)]
        if "init" in args[:2]:
            return await asyncio.to_thread(
                self._git, [*args[:1], "init", "--bare", "-b", "main", args[-1]]
            )
        if "-C" in args and args[args.index("-C") + 2] == "push":
            return self._push(job_id, args)
        self._sync_main()
        return await asyncio.to_thread(self._git, args)

    def _push(self, job_id: UUID, args: list[str]) -> ExecResult:
        lease = next(a for a in args if a.startswith("--force-with-lease="))
        ref, _, expected = lease.removeprefix("--force-with-lease=").partition(":")
        branch = ref.removeprefix("refs/heads/")
        live = self.gh.branches.get(branch, "")
        if live != expected:
            return ExecResult(1, "", "! [rejected] (stale info)", False, 0.0)
        repo = str(self._dir(job_id) / "repo.git")
        sha = self._git(["git", "-C", repo, "rev-parse", ref]).stdout.strip()
        r = self._git(
            ["git", "-C", repo, "push", "-q", "--force", str(self.origin), f"{ref}:{ref}"]
        )
        if r.exit_code != 0:
            return r
        self.gh.branches[branch] = sha
        self.gh.pushes += 1
        self.gh.push_leases.append(expected)
        self.gh.maybe_crash("push")
        return ExecResult(0, "", "", False, 0.0)

    async def put_file(self, job_id: UUID, path: str, src: Path) -> None:
        dest = Path(self._map(job_id, path))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    async def get_file(self, job_id: UUID, path: str, dest: Path, *, max_bytes: int) -> int:
        src = Path(self._map(job_id, path))
        if not src.is_file():
            raise FileNotFoundError(path)
        if src.stat().st_size > max_bytes:
            raise TransferTooLarge(max_bytes)
        shutil.copyfile(src, dest)
        return src.stat().st_size

    def wipe_volume(self, job_id: UUID) -> None:
        shutil.rmtree(self._dir(job_id), ignore_errors=True)
