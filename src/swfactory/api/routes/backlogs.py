"""Backlogs: list, show, resume, skip, cancel and import."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.api.access import app_role
from swfactory.api.auth import ROLE_RANK, Principal, require
from swfactory.api.routes.board import column_for
from swfactory.backlogs import (
    BacklogError,
    RequirementItem,
    blocked_job,
    cancel_backlog,
    format_backlog_ref,
    import_backlog,
    parse_backlog_ref,
    refresh_backlog,
    skip_blocked,
)
from swfactory.commands import CommandRefused, format_job_ref, retry_offered, submit_command
from swfactory.db.repo import latest_waiting
from swfactory.db.tables import App, Backlog, Job
from swfactory.definition.store import NoActiveDefinition
from swfactory.domain.types import TERMINAL_STAGES, HumanAction, Stage
from swfactory.queue import UnknownApp

__all__ = ["apply_backlog_action", "router"]

_TERMINAL = {st.value for st in TERMINAL_STAGES}


class BacklogAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["resume", "skip", "cancel"]


class ImportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    feature: str = Field(min_length=1)
    acceptance: list[str] = []


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app: str
    title: str = Field(min_length=1)
    items: list[ImportItem]


router = APIRouter()


def _deps(request: Request) -> Any:
    return request.app.state.deps


async def _app_access(request: Request, slug: str) -> Any:
    try:
        _, defn = await _deps(request).definitions.active()
    except NoActiveDefinition:
        return None
    app_def = defn.factory.apps.get(slug)
    return None if app_def is None else getattr(app_def, "access", None)


async def _role(request: Request, principal: Principal, slug: str, need: str) -> str:
    """The principal's role on the app; 404 when hidden, 403 when below `need`."""
    role = app_role(
        principal.role,
        principal.email,
        await _app_access(request, slug),
        mode=request.app.state.mode,
    )
    if role is None:
        raise HTTPException(status_code=404, detail="backlog not found")
    if ROLE_RANK[role] < ROLE_RANK[need]:
        raise HTTPException(status_code=403, detail=f"{need} role required for app '{slug}'")
    return role


async def _find(s: AsyncSession, ref: str) -> tuple[Backlog, str]:
    try:
        key = parse_backlog_ref(ref)
    except ValueError:
        raise HTTPException(status_code=404, detail="backlog not found") from None
    q = select(Backlog, App.slug).join(App, App.id == Backlog.app_id)
    q = (
        q.where(Backlog.id == key)
        if isinstance(key, UUID)
        else q.where(App.slug == key[0], Backlog.seq == key[1])
    )
    row = (await s.execute(q)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="backlog not found")
    return row[0], row[1]


async def _hidden_ok(request: Request, principal: Principal, slug: str) -> bool:
    role = app_role(
        principal.role,
        principal.email,
        await _app_access(request, slug),
        mode=request.app.state.mode,
    )
    return role is not None


async def _row(s: AsyncSession, bl: Backlog, slug: str) -> dict[str, Any]:
    total, merged = (
        await s.execute(
            select(
                func.count(),
                func.count().filter(Job.stage == Stage.MERGED.value),
            ).where(Job.backlog_id == bl.id, Job.backlog_pos.is_not(None))
        )
    ).one()
    req_ref = None
    if bl.requirements_job_id is not None:
        seq = await s.scalar(select(Job.seq).where(Job.id == bl.requirements_job_id))
        req_ref = None if seq is None else format_job_ref(slug, seq)
    return {
        "backlog_id": str(bl.id),
        "ref": format_backlog_ref(slug, bl.seq),
        "app": slug,
        "title": bl.title,
        "status": bl.status,
        "created_by": bl.created_by,
        "approved_by": bl.approved_by,
        "approved_at": None if bl.approved_at is None else bl.approved_at.isoformat(),
        "updated_at": bl.updated_at.isoformat(),
        "jobs_total": int(total),
        "jobs_merged": int(merged),
        "requirements_job_ref": req_ref,
    }


@router.get("/backlogs")
async def list_backlogs(
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
    app: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    async with _deps(request).sessions() as s:
        q = (
            select(Backlog, App.slug)
            .join(App, App.id == Backlog.app_id)
            .order_by(Backlog.created_at.desc(), Backlog.seq.desc())
        )
        if app is not None:
            q = q.where(App.slug == app)
        if status is not None:
            q = q.where(Backlog.status == status)
        out = []
        for bl, slug in (await s.execute(q)).all():
            if await _hidden_ok(request, principal, slug):
                out.append(await _row(s, bl, slug))
        return out


@router.get("/backlogs/{ref}")
async def show_backlog(
    request: Request, ref: str, principal: Annotated[Principal, Depends(require("viewer"))]
) -> dict[str, Any]:
    async with _deps(request).sessions() as s:
        bl, slug = await _find(s, ref)
        await _role(request, principal, slug, "viewer")
        out = await _row(s, bl, slug)
        jobs = (
            await s.scalars(
                select(Job)
                .where(Job.backlog_id == bl.id, Job.backlog_pos.is_not(None))
                .order_by(Job.backlog_pos)
            )
        ).all()
        out["items"] = [
            {
                "pos": j.backlog_pos,
                "id": (j.source_ref or "").split(":", 2)[-1],
                "title": j.title,
                "job_ref": format_job_ref(slug, j.seq),
                "stage": j.stage,
                "column": column_for(Stage(j.stage)),
                "cost_usd": f"{j.cost_usd:.6f}",
            }
            for j in jobs
        ]
        blocked = await blocked_job(s, bl.id)
        out["blocked_job"] = None if blocked is None else format_job_ref(slug, blocked.seq)
        req = None
        if bl.requirements_job_id is not None:
            rj = await s.get(Job, bl.requirements_job_id)
            if rj is not None:
                waiting = await latest_waiting(s, rj.id) if rj.stage == "needs_human" else None
                req = {
                    "ref": format_job_ref(slug, rj.seq),
                    "stage": rj.stage,
                    "gate_reason": rj.gate_reason,
                    "retry_offered": retry_offered(None if waiting is None else waiting[1]),
                }
        out["requirements_job"] = req
        return out


async def apply_backlog_action(s: AsyncSession, *, backlog_id: UUID, action: str, by: str) -> str:
    """Resume, skip or cancel; raises BacklogError or CommandRefused for a refused action."""
    if action == "cancel":
        jobs = await cancel_backlog(s, backlog_id=backlog_id)
        for j in jobs:
            await submit_command(
                s, job_id=j.id, action=HumanAction.CANCEL, note=None, requested_by=by
            )
        return "cancelled"
    if action == "skip":
        job = await skip_blocked(s, backlog_id=backlog_id)
        if job.stage not in _TERMINAL:
            await submit_command(
                s, job_id=job.id, action=HumanAction.CANCEL, note=None, requested_by=by
            )
        return str(await s.scalar(select(Backlog.status).where(Backlog.id == backlog_id)))
    status = await refresh_backlog(s, backlog_id)
    if status != "paused":
        return status
    job = await blocked_job(s, backlog_id)
    if job is None:
        return status
    slug = str(await s.scalar(select(App.slug).where(App.id == job.app_id)))
    ref = format_job_ref(slug, job.seq)
    if job.stage == Stage.FAILED.value:
        retry = True
    elif job.stage == Stage.NEEDS_HUMAN.value:
        waiting = await latest_waiting(s, job.id)
        if not retry_offered(None if waiting is None else waiting[1]):
            raise BacklogError(f"job {ref} is waiting for a person")
        retry = True
    else:
        raise BacklogError(f"job {ref} is {job.stage}; skip it or cancel the backlog")
    if retry:
        await submit_command(s, job_id=job.id, action=HumanAction.RETRY, note=None, requested_by=by)
    return status


@router.post("/backlogs/import", status_code=201)
async def import_backlog_route(
    request: Request,
    body: ImportBody,
    principal: Annotated[Principal, Depends(require("submitter"))],
) -> JSONResponse:
    d = _deps(request)
    try:
        _, defn = await d.definitions.active()
    except NoActiveDefinition:
        raise HTTPException(status_code=503, detail="no active definition") from None
    await _role(request, principal, body.app, "submitter")
    limit = defn.factory.defaults.max_backlog_items
    if not body.items or len(body.items) > limit:
        raise HTTPException(status_code=422, detail=f"send between 1 and {limit} items")
    items = [RequirementItem(i.id, i.title, i.feature, (), tuple(i.acceptance)) for i in body.items]
    try:
        async with d.sessions.begin() as s:
            bl, jobs = await import_backlog(
                s,
                app_slug=body.app,
                title=body.title,
                items=items,
                requested_by=principal.email,
            )
            out = {
                "ref": format_backlog_ref(body.app, bl.seq),
                "backlog_id": str(bl.id),
                "status": bl.status,
                "jobs": [
                    {
                        "ref": format_job_ref(body.app, j.seq),
                        "job_id": str(j.id),
                        "pos": j.backlog_pos,
                    }
                    for j in jobs
                ],
            }
    except UnknownApp:
        raise HTTPException(status_code=422, detail=f"unknown app '{body.app}'") from None
    except BacklogError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return JSONResponse(out, status_code=201)


@router.post("/backlogs/{ref}/actions")
async def backlog_action(
    request: Request,
    ref: str,
    body: BacklogAction,
    principal: Annotated[Principal, Depends(require("approver"))],
) -> dict[str, str]:
    d = _deps(request)
    try:
        async with d.sessions.begin() as s:
            bl, slug = await _find(s, ref)
            await _role(request, principal, slug, "approver")
            status = await apply_backlog_action(
                s, backlog_id=bl.id, action=body.action, by=principal.email
            )
    except (BacklogError, CommandRefused) as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return {"status": status}
