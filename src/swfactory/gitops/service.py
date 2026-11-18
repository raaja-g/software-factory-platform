"""GitService protocol and its result types."""

from __future__ import annotations

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
