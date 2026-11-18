"""In-memory GitService fake."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from swfactory.gitops.service import (
    REASON_HEAD_MISMATCH,
    Delivery,
    DiffStat,
    GitService,
)

if TYPE_CHECKING:
    from swfactory.db.tables import Job
    from swfactory.definition.models import AppDef
    from swfactory.sandbox.base import Sandbox

ZERO = "0" * 40


class FakeGitService(GitService):
    def __init__(
        self,
        conflict: bool = False,
        fail_on: str | None = None,
        diff_paths: Sequence[str] = (),
        state_path: Path | None = None,
        deliver_failure: str | None = None,
    ) -> None:
        self.conflict = conflict
        self.fail_on = fail_on
        self.diff_paths = tuple(diff_paths)
        self.state_path = state_path
        self.deliver_failure = deliver_failure
        self.calls: list[str] = []
        self.expected_heads: list[str] = []

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_on == name:
            raise RuntimeError(f"fake git failure on {name}")

    def _load(self) -> dict[str, dict[str, object]]:
        if self.state_path is not None and self.state_path.exists():
            return json.loads(self.state_path.read_text())["delivered"]
        return {}

    async def prepare(self, job: Job, app: AppDef, box: Sandbox) -> str:
        self._call("prepare")
        return ZERO

    async def rebase(self, job: Job, app: AppDef, box: Sandbox) -> bool:
        self._call("rebase")
        return not self.conflict

    async def diff_stat(self, box: Sandbox, base: str) -> DiffStat:
        self._call("diff_stat")
        return DiffStat(len(self.diff_paths), 0, 0, self.diff_paths)

    async def deliver(
        self, job: Job, app: AppDef, box: Sandbox, *, draft: bool, expected_head: str
    ) -> Delivery:
        self._call("deliver")
        self.expected_heads.append(expected_head)
        key = str(job.id)
        saved = self._load()
        if key in saved:
            d = saved[key]
            return Delivery(
                d["status"],  # type: ignore[arg-type]
                d["pr_url"],  # type: ignore[arg-type]
                d["merge_sha"],  # type: ignore[arg-type]
                d["detail"],  # type: ignore[arg-type]
            )
        if self.deliver_failure is not None:
            return Delivery("failed", None, None, self.deliver_failure)
        if self.conflict:
            return Delivery("conflict", None, None, REASON_HEAD_MISMATCH)
        result = Delivery("merged", None, "f" * 40, "fake")
        if self.state_path is not None:
            saved[key] = {
                "status": result.status,
                "pr_url": result.pr_url,
                "merge_sha": result.merge_sha,
                "detail": result.detail,
            }
            self.state_path.write_text(json.dumps({"delivered": saved}))
        return result

    async def keep_branch(self, job: Job, app: AppDef, box: Sandbox) -> None:
        self._call("keep_branch")

    async def base_head(self, job: Job, app: AppDef) -> str:
        self._call("base_head")
        return ZERO

    async def withdraw(self, job: Job, app: AppDef, *, note: str) -> None:
        self._call("withdraw")
