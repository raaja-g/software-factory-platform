"""Reference checks: a task's hidden checks must fail at the base and pass on the merge."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.db.tables import BenchTaskCheck, Job
from swfactory.definition.models import BenchTaskDef, Definition

REFERENCE_PREFIX = "bench-check:"
REFERENCE_LOG = "bench/reference-checks.log"


def is_reference_ref(source_ref: str | None) -> bool:
    return bool(source_ref) and str(source_ref).startswith(REFERENCE_PREFIX)


def reference_source_ref(key: str) -> str:
    return f"{REFERENCE_PREFIX}{key}"


def reference_branch(key: str, task: str) -> str:
    return f"bench/check-{key[:8]}/{task}"


def task_check_key(suite: str, task: BenchTaskDef) -> str:
    """Content key of a task's checks: any change to the repo, commits or checks makes a new one."""
    payload = {
        "repo": task.repo,
        "base_sha": task.base_sha,
        "merge_sha": task.merge_sha,
        "check_command": task.check_command,
        "files": [[p, task.check_files[p]] for p in sorted(task.check_files)],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckVerdict:
    valid: bool
    reason: str


def reference_verdict(
    at_merge: Mapping[str, int | None], at_base: Mapping[str, int | None]
) -> CheckVerdict:
    for path in sorted(at_merge):
        code = at_merge[path]
        if code is None:
            return CheckVerdict(False, f"{path} could not run on the merged result")
        if code != 0:
            return CheckVerdict(False, f"{path} fails on the merged result (exit {code})")
        base = at_base.get(path)
        if base == 0:
            return CheckVerdict(False, f"{path} passes at the base commit")
        if base is None:
            return CheckVerdict(False, f"{path} could not run at the base commit")
    return CheckVerdict(True, "")


async def reference_task(
    sessions: async_sessionmaker[AsyncSession], defn: Definition, job_id: UUID
) -> tuple[str, str, BenchTaskDef] | None:
    """(check key, task name, task) of a reference job, found through its check row."""
    async with sessions() as s:
        job = await s.get(Job, job_id)
        if job is None or not is_reference_ref(job.source_ref):
            return None
        key = str(job.source_ref)[len(REFERENCE_PREFIX) :]
        row = await s.get(BenchTaskCheck, key)
    if row is None:
        return None
    suite = defn.benchmarks.get(row.suite)
    task = None if suite is None else suite.tasks.get(row.task)
    return None if task is None else (key, row.task, task)


async def store_verdict(
    sessions: async_sessionmaker[AsyncSession], key: str, verdict: CheckVerdict, detail: Any
) -> None:
    from datetime import UTC, datetime

    async with sessions.begin() as s:
        row = await s.get(BenchTaskCheck, key)
        if row is not None:
            row.status = "valid" if verdict.valid else "invalid"
            row.detail = {**detail, "reason": verdict.reason}
            row.finished_at = datetime.now(UTC)
