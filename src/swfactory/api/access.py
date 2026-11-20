"""Per-app access lists: the role a principal holds on one app, and job-route guards."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from swfactory.api.auth import ROLE_RANK, Principal, require
from swfactory.db.repo import JobRefError, resolve_job_ref
from swfactory.db.tables import App, Job
from swfactory.definition.store import NoActiveDefinition
from swfactory.domain.types import FactoryMode

if TYPE_CHECKING:
    from swfactory.definition.models import AppAccess, Definition

__all__ = ["JobAccess", "app_role", "authorize_job", "job_access", "visible_apps"]


def app_role(role: str, email: str, access: AppAccess | None, *, mode: FactoryMode) -> str | None:
    """The lower of the instance role and the app list's cap; None when not listed."""
    if mode is FactoryMode.PERSONAL or access is None:
        return role
    who = email.strip().lower()
    if who in access.approvers:
        return role
    if who in access.submitters:
        cap = "submitter"
    elif who in access.viewers:
        cap = "viewer"
    else:
        return None
    return cap if ROLE_RANK[cap] < ROLE_RANK.get(role, 0) else role


def visible_apps(
    defn: Definition, principal: Principal, *, mode: FactoryMode
) -> frozenset[str] | None:
    """Slugs the principal may see; None when no app restricts access (every app)."""
    apps = defn.factory.apps
    if all(a.access is None for a in apps.values()):
        return None
    return frozenset(
        slug
        for slug, a in apps.items()
        if app_role(principal.role, principal.email, a.access, mode=mode) is not None
    )


@dataclass(frozen=True, slots=True)
class JobAccess:
    job: Job
    app_slug: str
    role: str


async def app_slug_of(s: Any, job: Job) -> str:
    if job.app_id is None:
        return ""
    return await s.scalar(select(App.slug).where(App.id == job.app_id)) or ""


async def authorize_job(request: Request, principal: Principal, job: Job, need: str) -> JobAccess:
    """404 for a hidden app, 403 below `need` on the job's app."""
    deps = request.app.state.deps
    mode: FactoryMode = request.app.state.mode
    async with deps.sessions() as s:
        slug = await app_slug_of(s, job)
    try:
        _, defn = await deps.definitions.active()
    except NoActiveDefinition:
        raise HTTPException(status_code=503, detail="no active definition") from None
    visible = visible_apps(defn, principal, mode=mode)
    app_def = defn.factory.apps.get(slug)
    role = app_role(
        principal.role, principal.email, None if app_def is None else app_def.access, mode=mode
    )
    if role is None or (visible is not None and slug not in visible):
        raise HTTPException(status_code=404, detail="job not found")
    if ROLE_RANK[role] < ROLE_RANK[need]:
        raise HTTPException(status_code=403, detail=f"{need} role required for app '{slug}'")
    return JobAccess(job, slug, role)


def job_access(need: str) -> Callable[..., Awaitable[JobAccess]]:
    """Dependency for `{ref}` routes: 404 for a hidden app, 403 below `need` on the app."""

    async def dependency(
        request: Request, ref: str, principal: Annotated[Principal, Depends(require("viewer"))]
    ) -> JobAccess:
        deps = request.app.state.deps
        async with deps.sessions() as s:
            try:
                job = await resolve_job_ref(s, ref)
            except JobRefError as e:
                raise HTTPException(status_code=404, detail=str(e)) from None
        return await authorize_job(request, principal, job, need)

    dependency._job_access = need  # type: ignore[attr-defined]  # noqa: SLF001
    return dependency
