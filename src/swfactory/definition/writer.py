"""Definition writers: personal mode commits locally, server mode opens a GitHub PR.

Both writers accept only `profiles/<name>.yaml` and `apps/<slug>.yaml`; nothing else in
the definition repo (factory.yaml, runners/) can be written through them.
"""

from __future__ import annotations

import asyncio
import base64
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from swfactory.gitops.service import SAFE_GIT_ENV, safe_git

_PATH_RE = re.compile(r"^(profiles|apps)/[a-z0-9][a-z0-9-]{0,39}\.yaml$")
_SLUG = r"[a-z0-9][a-z0-9-]{0,39}"
BENCH_PATH_RE = re.compile(
    rf"^benchmarks/{_SLUG}/(suite\.yaml|tasks/{_SLUG}\.yaml|checks/{_SLUG}(/[A-Za-z0-9_][A-Za-z0-9._-]*)+)$"
)
_STRIP_AUTHOR = re.compile(r"[<>\x00-\x1f\x7f]")
_BOT = {
    "GIT_COMMITTER_NAME": "factory-bot",
    "GIT_COMMITTER_EMAIL": "factory-bot@localhost",
    "GIT_AUTHOR_NAME": "factory-bot",
    "GIT_AUTHOR_EMAIL": "factory-bot@localhost",
}


class DefinitionWriteError(Exception):
    """A definition file could not be saved."""


@dataclass(frozen=True, slots=True)
class SaveResult:
    mode: Literal["commit", "pr"]
    sha: str | None
    pr_url: str | None


class DefinitionWriter(Protocol):
    async def save_file(
        self, path: str, content: str, *, message: str, author: str
    ) -> SaveResult: ...

    async def save_files(
        self, files: Mapping[str, str], *, message: str, author: str
    ) -> SaveResult: ...


def _check_bench_paths(files: Mapping[str, str]) -> None:
    if not files:
        raise DefinitionWriteError("no files to write")
    for path in files:
        if not BENCH_PATH_RE.fullmatch(path):
            raise DefinitionWriteError(
                f"refusing to write {path!r}: only benchmarks/<suite>/ suite, task and check files"
            )


def _check_path(path: str) -> None:
    if not _PATH_RE.fullmatch(path):
        raise DefinitionWriteError(
            f"refusing to write {path!r}: only profiles/<name>.yaml and apps/<slug>.yaml"
        )


def _clean_author(author: str) -> str:
    cleaned = _STRIP_AUTHOR.sub("", author).strip()
    return cleaned or "unknown"


class LocalGitDefinitionWriter:
    def __init__(self, repo_path: Path) -> None:
        self._repo = Path(repo_path)

    def _git(self, *args: str) -> str:
        import os

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            **SAFE_GIT_ENV,
            **_BOT,
        }
        proc = subprocess.run(  # noqa: S603
            safe_git(*args), cwd=self._repo, env=env, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise DefinitionWriteError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def _save(self, path: str, content: str, message: str, author: str) -> SaveResult:
        _check_path(path)
        root = self._repo.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root / path.split("/")[0]):
            raise DefinitionWriteError(f"refusing to write {path!r}: path escapes the repository")
        if self._git("status", "--porcelain", "--", path).strip():
            raise DefinitionWriteError(
                f"{path} has uncommitted changes; commit or discard them first"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._git("add", "--", path)
        if self._git("status", "--porcelain", "--", path).strip():
            who = _clean_author(author)
            self._git("commit", "-m", message, f"--author={who} <{who}>", "--", path)
        return SaveResult("commit", self._git("rev-parse", "HEAD").strip(), None)

    async def save_file(self, path: str, content: str, *, message: str, author: str) -> SaveResult:
        _check_path(path)
        return await asyncio.to_thread(self._save, path, content, message, author)

    def _save_many(self, files: Mapping[str, str], message: str, author: str) -> SaveResult:
        root = self._repo.resolve()
        for path in files:
            target = (root / path).resolve()
            if not target.is_relative_to(root / "benchmarks"):
                raise DefinitionWriteError(f"refusing to write {path!r}: path escapes benchmarks/")
            if self._git("status", "--porcelain", "--", path).strip():
                raise DefinitionWriteError(
                    f"{path} has uncommitted changes; commit or discard them first"
                )
        paths = list(files)
        for path in paths:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[path], encoding="utf-8")
        self._git("add", "--", *paths)
        if self._git("status", "--porcelain", "--", *paths).strip():
            who = _clean_author(author)
            self._git("commit", "-m", message, f"--author={who} <{who}>", "--", *paths)
        return SaveResult("commit", self._git("rev-parse", "HEAD").strip(), None)

    async def save_files(
        self, files: Mapping[str, str], *, message: str, author: str
    ) -> SaveResult:
        _check_bench_paths(files)
        return await asyncio.to_thread(self._save_many, dict(files), message, author)


class GitHubClientLike(Protocol):
    """The slice of a GitHub client the writer uses (M8 adapts the real one to it)."""

    async def get_branch_sha(self, repo: str, branch: str) -> str: ...
    async def get_file_sha(self, repo: str, path: str, ref: str) -> str | None: ...
    async def create_ref(self, repo: str, ref: str, sha: str) -> Any: ...
    async def create_or_update_file_contents(
        self,
        repo: str,
        path: str,
        *,
        message: str,
        content: str,
        branch: str,
        sha: str | None,
        author: str,
    ) -> Any: ...
    async def create_pull(
        self, repo: str, *, title: str, head: str, base: str, body: str
    ) -> str: ...


class GitHubDefinitionWriter:
    def __init__(
        self,
        client_factory: Callable[[], Awaitable[Any]],
        repo: str,
        base: str,
        *,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._factory = client_factory
        self._repo = repo
        self._base = base
        self._interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def _mutate(self, call: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        if self._last is not None:
            wait = self._interval - (self._clock() - self._last)
            if wait > 0:
                await self._sleep(wait)
        try:
            return await call(*args, **kwargs)
        finally:
            self._last = self._clock()

    async def save_file(self, path: str, content: str, *, message: str, author: str) -> SaveResult:
        _check_path(path)
        kind, name = path.removesuffix(".yaml").split("/")
        kind = "profile" if kind == "profiles" else "app"
        branch = f"edit/{kind}-{name}-{datetime.now(UTC).strftime('%Y%m%d%H%M')}"
        who = _clean_author(author)
        async with self._lock:
            try:
                gh = await self._factory()
                base_sha = await gh.get_branch_sha(self._repo, self._base)
                await self._mutate(gh.create_ref, self._repo, f"refs/heads/{branch}", base_sha)
                old = await gh.get_file_sha(self._repo, path, self._base)
                await self._mutate(
                    gh.create_or_update_file_contents,
                    self._repo,
                    path,
                    message=message,
                    content=base64.b64encode(content.encode()).decode(),
                    branch=branch,
                    sha=old,
                    author=who,
                )
                url = await self._mutate(
                    gh.create_pull,
                    self._repo,
                    title=message,
                    head=branch,
                    base=self._base,
                    body=f"Edited by {who} through the factory UI.",
                )
            except DefinitionWriteError:
                raise
            except Exception as exc:
                raise DefinitionWriteError(f"GitHub save failed: {type(exc).__name__}") from exc
        return SaveResult("pr", None, str(url))

    async def save_files(
        self, files: Mapping[str, str], *, message: str, author: str
    ) -> SaveResult:
        _check_bench_paths(files)
        suite = next(iter(files)).split("/")[1]
        branch = f"edit/bench-{suite}-{datetime.now(UTC).strftime('%Y%m%d%H%M')}"
        who = _clean_author(author)
        async with self._lock:
            try:
                gh = await self._factory()
                base_sha = await gh.get_branch_sha(self._repo, self._base)
                await self._mutate(gh.create_ref, self._repo, f"refs/heads/{branch}", base_sha)
                for path, content in files.items():
                    old = await gh.get_file_sha(self._repo, path, self._base)
                    await self._mutate(
                        gh.create_or_update_file_contents,
                        self._repo,
                        path,
                        message=message,
                        content=base64.b64encode(content.encode()).decode(),
                        branch=branch,
                        sha=old,
                        author=who,
                    )
                url = await self._mutate(
                    gh.create_pull,
                    self._repo,
                    title=message,
                    head=branch,
                    base=self._base,
                    body=f"Benchmark files added by {who} through the factory.",
                )
            except DefinitionWriteError:
                raise
            except Exception as exc:
                raise DefinitionWriteError(f"GitHub save failed: {type(exc).__name__}") from exc
        return SaveResult("pr", None, str(url))
