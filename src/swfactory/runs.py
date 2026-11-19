"""Point-and-run: a run request is recorded by the api; a worker tick does the rest.

The api cannot read the person's folders, so it only inserts a ``run_requests`` row. The
orchestrator registers the repo (once), then starts a requirements job for the requirement.
There is no NOTIFY: the worker polls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.apps import (
    RegisterDeps,
    RegisterError,
    RegistrationPending,
    find_registered,
    parse_repo,
    register_repo,
)
from swfactory.backlogs import create_backlog
from swfactory.db.tables import RunRequest
from swfactory.definition.models import PROFILE_NAME_RE, Budgets
from swfactory.definition.store import NoActiveDefinition, load_active
from swfactory.gitops.folder import FolderError
from swfactory.queue import UnknownApp

if TYPE_CHECKING:
    from swfactory.db.tables import Job

log = structlog.get_logger(__name__)

MAX_REQUIREMENT_BYTES = 200_000
WAIT_RETRY_S = 10.0


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunDeps:
    register: RegisterDeps
    now: Callable[[], datetime] = field(default=_now)


async def create_run_request(
    s: AsyncSession,
    *,
    repo: str,
    requirement: str,
    profile: str | None,
    ci_safe: bool,
    budget_usd: Decimal | None,
    requested_by: str,
) -> UUID:
    """Insert a pending request in the caller's transaction; ValueError for bad input."""
    if not requirement.strip():
        raise ValueError("the requirement is empty")
    if len(requirement.encode("utf-8")) > MAX_REQUIREMENT_BYTES:
        raise ValueError(f"the requirement is over {MAX_REQUIREMENT_BYTES} bytes")
    parse_repo(repo, host_home=None)  # RegisterError is a ValueError
    if profile is not None and not PROFILE_NAME_RE.fullmatch(profile):
        raise ValueError(f"'{profile}' is not a profile name")
    if budget_usd is not None:
        if not budget_usd.is_finite() or budget_usd <= 0:
            raise ValueError("the budget must be a positive number of USD")
        found = await load_active(s)
        limit = found[1].factory.budgets.job_max_usd if found else Budgets().job_max_usd
        if budget_usd > limit:
            raise ValueError(f"the budget is above the job maximum of {limit} USD")
    row = RunRequest(
        repo=repo,
        requirement=requirement,
        profile=profile,
        ci_safe=ci_safe,
        budget_usd=budget_usd,
        requested_by=requested_by,
        status="pending",
        updated_at=_now(),
    )
    s.add(row)
    await s.flush()
    return row.id


async def start_requirements_job(
    s: AsyncSession,
    *,
    app_slug: str,
    requirement: str,
    requested_by: str,
    profile_name: str | None,
    budget_usd: Decimal | None,
) -> tuple[UUID, Job]:
    """Create the backlog record and its requirements job in the caller's transaction."""
    bl, job = await create_backlog(
        s,
        app_slug=app_slug,
        requirement=requirement,
        requested_by=requested_by,
        profile_name=profile_name,
        budget_usd=budget_usd,
    )
    return bl.id, job


async def _claim(s: AsyncSession, now: datetime) -> RunRequest | None:
    stale = now - timedelta(seconds=WAIT_RETRY_S)
    return await s.scalar(
        select(RunRequest)
        .where(
            or_(
                RunRequest.status == "pending",
                (RunRequest.status == "waiting") & (RunRequest.updated_at <= stale),
            )
        )
        .order_by(RunRequest.created_at, RunRequest.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )


def _set(row: RunRequest, now: datetime, status: str, detail: str | None) -> None:
    row.status = status
    row.detail = detail
    row.updated_at = now


async def _resolve_slug(s: AsyncSession, row: RunRequest, deps: RunDeps) -> str | None:
    """The app slug for the row's repo, registering the repo first for a pending request.

    Returns None while the registration waits for its definition pull request."""
    ref = parse_repo(row.repo, host_home=deps.register.settings.host_home)
    if row.status == "pending":
        await register_repo(
            s, repo=row.repo, ci_safe=row.ci_safe, requested_by=row.requested_by, deps=deps.register
        )
    found = await load_active(s)
    if found is None:
        raise RegisterError("no definition is active yet")
    hit = find_registered(found[1], ref)
    return None if hit is None else hit[0]


async def run_request_tick(
    sessions: async_sessionmaker[AsyncSession], deps: RunDeps, *, worker_id: str
) -> bool:
    """Handle one pending (or stale waiting) request; True when one was started or failed."""
    async with sessions() as s, s.begin():
        now = deps.now()
        row = await _claim(s, now)
        if row is None:
            return False
        log.info("run_request.claimed", request_id=str(row.id), worker_id=worker_id)
        try:
            slug = await _resolve_slug(s, row, deps)
        except RegistrationPending as pending:
            _set(row, now, "waiting", pending.pr_url)
            return True
        except (RegisterError, FolderError, NoActiveDefinition) as exc:
            _set(row, now, "failed", str(exc))
            return True
        if slug is None:  # a waiting request whose definition PR is not merged yet
            _set(row, now, "waiting", row.detail)
            return False
        try:
            async with s.begin_nested():
                backlog_id, job = await start_requirements_job(
                    s,
                    app_slug=slug,
                    requirement=row.requirement,
                    requested_by=row.requested_by,
                    profile_name=row.profile,
                    budget_usd=row.budget_usd,
                )
        except (UnknownApp, ValueError) as exc:
            _set(row, now, "failed", str(exc))
            return True
        row.app_slug = slug
        row.backlog_id = backlog_id
        row.job_id = job.id
        _set(row, now, "started", None)
        return True


def build_run_deps(settings: Any, definitions: Any) -> RunDeps | None:
    """The worker's RunDeps, or None when this deployment cannot write definition files.

    Personal mode commits `apps/<slug>.yaml` to the local definition checkout; server mode
    needs a GitHub writer, which the deployment supplies elsewhere."""
    import asyncio
    from pathlib import Path

    from swfactory.definition.loader import load_definition
    from swfactory.definition.models import Definition
    from swfactory.definition.writer import LocalGitDefinitionWriter
    from swfactory.domain.types import FactoryMode

    source = settings.definition_source
    if settings.mode is not FactoryMode.PERSONAL or not source or not Path(source).is_dir():
        return None

    async def sync(_s: AsyncSession) -> Definition:
        result = await asyncio.to_thread(
            load_definition, source, ref=settings.definition_ref, mode=settings.mode
        )
        if result.errors or result.definition is None:
            raise RegisterError(
                "the definition does not validate: " + "; ".join(map(str, result.errors))
            )
        await definitions.save(result)
        return result.definition

    register = RegisterDeps.from_settings(settings, LocalGitDefinitionWriter(Path(source)), sync)
    return RunDeps(register)


async def get_run_view(s: AsyncSession, request_id: UUID) -> dict[str, Any] | None:
    """The request as the api shows it, plus its owner; None when there is no such row."""
    from swfactory.backlogs import format_backlog_ref
    from swfactory.commands import format_job_ref
    from swfactory.db.tables import Backlog, Job

    row = await s.get(RunRequest, request_id)
    if row is None:
        return None
    backlog = job_ref = None
    if row.app_slug is not None and row.backlog_id is not None:
        seq = await s.scalar(select(Backlog.seq).where(Backlog.id == row.backlog_id))
        if seq is not None:
            backlog = format_backlog_ref(row.app_slug, seq)
    if row.app_slug is not None and row.job_id is not None:
        jseq = await s.scalar(select(Job.seq).where(Job.id == row.job_id))
        if jseq is not None:
            job_ref = format_job_ref(row.app_slug, jseq)
    return {
        "status": row.status,
        "app": row.app_slug,
        "backlog": backlog,
        "job_ref": job_ref,
        "detail": row.detail,
        "requested_by": row.requested_by,
    }


__all__ = [
    "MAX_REQUIREMENT_BYTES",
    "RunDeps",
    "build_run_deps",
    "create_run_request",
    "get_run_view",
    "run_request_tick",
    "start_requirements_job",
]
