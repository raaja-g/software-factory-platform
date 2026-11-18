"""GitService protocol and its result types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from swfactory.db.tables import Job
    from swfactory.definition.models import AppDef
    from swfactory.sandbox.base import Sandbox


@dataclass(frozen=True, slots=True)
class DiffStat:
    files: int
    insertions: int
    deletions: int
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Delivery:
    status: Literal["merged", "pr_open", "conflict", "failed"]
    pr_url: str | None
    merge_sha: str | None
    detail: str


class GitService:
    async def prepare(self, job: Job, app: AppDef, box: Sandbox) -> str:
        raise NotImplementedError

    async def rebase(self, job: Job, app: AppDef, box: Sandbox) -> bool:
        raise NotImplementedError

    async def diff_stat(self, box: Sandbox, base: str) -> DiffStat:
        raise NotImplementedError

    async def deliver(
        self, job: Job, app: AppDef, box: Sandbox, *, draft: bool, expected_head: str
    ) -> Delivery:
        raise NotImplementedError

    async def keep_branch(self, job: Job, app: AppDef, box: Sandbox) -> None:
        raise NotImplementedError

    async def base_head(self, job: Job, app: AppDef) -> str:
        raise NotImplementedError

    async def withdraw(self, job: Job, app: AppDef, *, note: str) -> None:
        raise NotImplementedError


REASON_HEAD_MISMATCH = "branch head differs from the approved head"
REASON_NOTHING_TO_DELIVER = "nothing to deliver"
REASON_FOLDER_DIRTY = "your folder has uncommitted changes"
REASON_FOLDER_MOVED = "main in your folder moved on"


SAFE_GIT_FLAGS: tuple[str, ...] = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.external=",
    "-c",
    "core.pager=cat",
)

SAFE_GIT_ENV: Mapping[str, str] = {
    "HOME": "/tmp/git-home",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
}


def safe_git(*args: str) -> list[str]:
    """A git argv that never runs hooks, fsmonitor, external diff or a pager."""
    return ["git", *SAFE_GIT_FLAGS, *args]


def app_slug(app: AppDef) -> str:
    """The app's slug: `AppDef.slug` when present, else the last part of `repo_url`."""
    slug = getattr(app, "slug", None)
    if isinstance(slug, str) and slug:
        return slug
    name = app.repo_url.rstrip("/").rsplit("/", 1)[-1]
    return name.removesuffix(".git")


class GitDispatcher(GitService):
    def __init__(self, by_kind: Mapping[str, GitService]) -> None:
        self.by_kind = by_kind

    def _for(self, app: AppDef) -> GitService:
        try:
            return self.by_kind[app.repo_kind]
        except KeyError:
            raise ValueError(f"no git service for repo kind {app.repo_kind!r}") from None

    async def prepare(self, job: Job, app: AppDef, box: Sandbox) -> str:
        return await self._for(app).prepare(job, app, box)

    async def rebase(self, job: Job, app: AppDef, box: Sandbox) -> bool:
        return await self._for(app).rebase(job, app, box)

    async def diff_stat(self, box: Sandbox, base: str) -> DiffStat:
        # No app is given, so the box's own repo is diffed the same way for every kind.
        for svc in self.by_kind.values():
            return await svc.diff_stat(box, base)
        raise ValueError("no git services configured")

    async def deliver(
        self, job: Job, app: AppDef, box: Sandbox, *, draft: bool, expected_head: str
    ) -> Delivery:
        return await self._for(app).deliver(job, app, box, draft=draft, expected_head=expected_head)

    async def keep_branch(self, job: Job, app: AppDef, box: Sandbox) -> None:
        await self._for(app).keep_branch(job, app, box)

    async def base_head(self, job: Job, app: AppDef) -> str:
        return await self._for(app).base_head(job, app)

    async def withdraw(self, job: Job, app: AppDef, *, note: str) -> None:
        if app.repo_kind == "local":
            return
        await self._for(app).withdraw(job, app, note=note)
