"""Jobs: listing, intake, detail, runs, events, artifacts and human actions."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text

from swfactory.api.access import JobAccess, app_role, job_access, visible_apps
from swfactory.api.auth import ROLE_RANK, Principal, require
from swfactory.artifacts import validate_artifact_path
from swfactory.backlogs import format_backlog_ref
from swfactory.commands import (
    CommandRefused,
    action_allowed,
    format_job_ref,
    retry_offered,
    submit_command,
)
from swfactory.db.repo import latest_waiting, list_jobs
from swfactory.db.tables import AgentRun, App, Backlog, Job, JobEvent
from swfactory.definition.store import NoActiveDefinition
from swfactory.domain.types import HumanAction, Stage
from swfactory.queue import UnknownApp, enqueue_job

__all__ = [
    "ACTION_ROLE",
    "ActionRequest",
    "BudgetRefused",
    "JobCreate",
    "action_allowed",
    "effective_budget",
    "retry_offered",
    "router",
]

ACTION_ROLE: dict[HumanAction, str] = {
    HumanAction.APPROVE: "approver",
    HumanAction.REWORK: "approver",
    HumanAction.REJECT: "approver",
    HumanAction.RETRY: "approver",
    HumanAction.ADD_BUDGET: "admin",
    HumanAction.CANCEL: "submitter",
}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BudgetRefused(ValueError):
    pass


def effective_budget(
    requested: Decimal | None, *, default: Decimal, job_max: Decimal, is_admin: bool
) -> Decimal:
    if requested is None:
        return default
    if requested > job_max:
        raise BudgetRefused(f"budget above the per-job maximum of {job_max}")
    if requested > default and not is_admin:
        raise BudgetRefused(f"budget above {default} needs an admin")
    return requested


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app: str
    feature: str = Field(min_length=1)
    title: str | None = None
    profile: str | None = None
    budget_usd: Decimal | None = Field(default=None, gt=0)
    source: Literal["dashboard", "cli", "backlog", "api"] = "api"
    source_ref: str | None = None


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: HumanAction
    note: str | None = None
    head_sha: str | None = Field(default=None, pattern="^[0-9a-f]{40}$")
    amount_usd: Decimal | None = Field(None, gt=0, max_digits=12, decimal_places=6)

    @model_validator(mode="after")
    def _rework_note(self) -> ActionRequest:
        if self.action is HumanAction.ADD_BUDGET and self.amount_usd is None:
            raise ValueError("add_budget needs amount_usd")
        if self.action is not HumanAction.ADD_BUDGET and self.amount_usd is not None:
            raise ValueError("only add_budget takes amount_usd")
        if self.action is HumanAction.REWORK and not (self.note or "").strip():
            raise ValueError("rework needs a note")
        return self


router = APIRouter()


def _deps(request: Request) -> Any:
    return request.app.state.deps


def _summary(j: Job, slug: str | None) -> dict[str, Any]:
    return {
        "job_id": str(j.id),
        "ref": format_job_ref(slug or "", j.seq),
        "app": slug,
        "title": j.title,
        "stage": j.stage,
        "kind": j.kind,
        "round": j.round,
        "branch": j.branch,
        "decision": j.decision,
        "gate_reason": j.gate_reason,
        "pr_url": j.pr_url,
        "cost_usd": f"{j.cost_usd:.6f}",
        "budget_usd": f"{j.budget_usd:.2f}",
        "requested_by": j.requested_by,
        "source": j.source,
        "created_at": j.created_at.astimezone(UTC).isoformat(),
        "updated_at": j.updated_at.astimezone(UTC).isoformat(),
    }


@router.get("/jobs")
async def get_jobs(
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
    app: str | None = None,
    stage: Stage | None = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    d = _deps(request)
    visible = None
    try:
        _, defn = await d.definitions.active()
        visible = visible_apps(defn, principal, mode=request.app.state.mode)
    except NoActiveDefinition:
        pass
    if visible is not None and app is not None and app not in visible:
        return []
    async with d.sessions() as s:
        jobs = await list_jobs(s, app_slug=app, stage=stage, limit=limit, app_slugs=visible)
        slugs = {a.id: a.slug for a in (await s.scalars(select(App))).all()}
    return [_summary(j, slugs.get(j.app_id)) for j in jobs]  # pyright: ignore[reportArgumentType]


@router.post("/jobs")
async def create_job(
    request: Request,
    body: JobCreate,
    principal: Annotated[Principal, Depends(require("submitter"))],
) -> Response:
    d = _deps(request)
    try:
        _, defn = await d.definitions.active()
    except NoActiveDefinition:
        raise HTTPException(status_code=503, detail="no active definition") from None
    app_def = defn.factory.apps.get(body.app)
    if app_def is None:
        raise HTTPException(status_code=422, detail=f"unknown app '{body.app}'")
    budgets = defn.factory.budgets
    role = app_role(principal.role, principal.email, app_def.access, mode=request.app.state.mode)
    if role is None or ROLE_RANK[role] < ROLE_RANK["submitter"]:
        raise HTTPException(status_code=403, detail=f"submitter role required for app '{body.app}'")
    try:
        budget = effective_budget(
            body.budget_usd,
            default=app_def.budget_usd or budgets.job_usd,
            job_max=budgets.job_max_usd,
            is_admin=ROLE_RANK[role] >= ROLE_RANK["admin"],
        )
    except BudgetRefused as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    try:
        async with d.sessions.begin() as s:
            if body.source_ref is not None:
                await s.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                    {"k": f"{body.app}:{body.source}:{body.source_ref}"},
                )
                existing = (
                    await s.execute(
                        select(Job.id, Job.seq)
                        .join(App, App.id == Job.app_id)
                        .where(
                            App.slug == body.app,
                            Job.source == body.source,
                            Job.source_ref == body.source_ref,
                        )
                        .order_by(Job.created_at)
                        .limit(1)
                    )
                ).first()
                if existing is not None:
                    return JSONResponse(
                        {"job_id": str(existing.id), "ref": format_job_ref(body.app, existing.seq)}
                    )
            job = await enqueue_job(
                s,
                app_slug=body.app,
                feature=body.feature,
                title=body.title,
                source=body.source,
                source_ref=body.source_ref,
                requested_by=principal.email,
                profile_name=body.profile,
                budget_usd=budget,
            )
            out = {"job_id": str(job.id), "ref": format_job_ref(body.app, job.seq)}
    except UnknownApp:
        raise HTTPException(status_code=422, detail=f"unknown app '{body.app}'") from None
    except NoActiveDefinition:
        raise HTTPException(status_code=503, detail="no active definition") from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return JSONResponse(out, status_code=201)


@router.get("/jobs/{ref}")
async def get_job(
    request: Request, ja: Annotated[JobAccess, Depends(job_access("viewer"))]
) -> dict[str, Any]:
    job, slug = ja.job, ja.app_slug
    async with _deps(request).sessions() as s:
        waiting = await latest_waiting(s, job.id) if job.stage == Stage.NEEDS_HUMAN else None
        out = _summary(job, slug)
        out.update(
            feature=job.feature,
            triage=job.triage,
            error=job.error,
            profile=job.profile_name,
            graph_version=job.graph_version,
            waiting=None if waiting is None else dict(waiting[1]),
            backlog_ref=None,
            backlog_pos=job.backlog_pos,
        )
        if job.backlog_id is not None:
            bseq = await s.scalar(select(Backlog.seq).where(Backlog.id == job.backlog_id))
            if bseq is not None:
                out["backlog_ref"] = format_backlog_ref(slug or "", bseq)
        wdata = None if waiting is None else waiting[1]
        out["allowed_actions"] = [
            a.value
            for a in HumanAction
            if action_allowed(a, stage=Stage(job.stage), retry_offered=retry_offered(wdata))
        ]
        return out


@router.get("/jobs/{ref}/runs")
async def get_runs(
    request: Request, ja: Annotated[JobAccess, Depends(job_access("viewer"))]
) -> list[dict[str, Any]]:
    job_id = ja.job.id
    async with _deps(request).sessions() as s:
        rows = (
            await s.scalars(
                select(AgentRun)
                .where(AgentRun.job_id == job_id)
                .order_by(AgentRun.started_at, AgentRun.attempt)
            )
        ).all()
    return [
        {
            "id": str(r.id),
            "role": r.role,
            "round": r.round,
            "attempt": r.attempt,
            "status": r.status,
            "verdict": r.verdict,
            "engine": r.engine,
            "engine_version": r.engine_version,
            "requested_model": r.requested_model,
            "used_models": r.used_models,
            "selection_mode": r.selection_mode,
            "model_alias": r.model_alias,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "cost_usd": None if r.cost_usd is None else f"{r.cost_usd:.6f}",
            "cost_source": r.cost_source,
            "started_at": r.started_at.astimezone(UTC).isoformat(),
            "finished_at": None
            if r.finished_at is None
            else r.finished_at.astimezone(UTC).isoformat(),
        }
        for r in rows
    ]


@router.get("/jobs/{ref}/events")
async def get_events(
    request: Request,
    ja: Annotated[JobAccess, Depends(job_access("viewer"))],
    after: int = 0,
    limit: Annotated[int, Field(ge=1, le=1000)] = 500,
) -> list[dict[str, Any]]:
    job_id = ja.job.id
    async with _deps(request).sessions() as s:
        rows = (
            await s.scalars(
                select(JobEvent)
                .where(JobEvent.job_id == job_id, JobEvent.id > after)
                .order_by(JobEvent.id)
                .limit(limit)
            )
        ).all()
    return [
        {"id": e.id, "at": e.at.astimezone(UTC).isoformat(), "kind": e.kind, "data": e.data}
        for e in rows
    ]


@router.get("/jobs/{ref}/artifacts")
async def get_artifacts(
    request: Request, ja: Annotated[JobAccess, Depends(job_access("viewer"))]
) -> list[dict[str, Any]]:
    job_id = ja.job.id
    metas = await _deps(request).artifacts.list(job_id)
    return [
        {
            "path": m.path,
            "kind": m.kind,
            "sha256": m.sha256,
            "size": m.size,
            "created_at": m.created_at.astimezone(UTC).isoformat(),
        }
        for m in metas
    ]


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


@router.get("/jobs/{ref}/artifacts/{path:path}")
async def get_artifact(
    request: Request, path: str, ja: Annotated[JobAccess, Depends(job_access("viewer"))]
) -> Response:
    try:
        validate_artifact_path(path)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid artifact path") from None
    job_id = ja.job.id
    store = _deps(request).artifacts
    meta = next((m for m in await store.list(job_id) if m.path == path), None)
    if meta is None:
        raise HTTPException(status_code=404, detail="no such artifact")
    name = _UNSAFE_NAME.sub("_", path.rsplit("/", 1)[-1])
    attach = {"Content-Disposition": f'attachment; filename="{name}"'}
    if meta.kind == "image":
        data = await store.get_bytes(job_id, path)
        if data is None:
            raise HTTPException(status_code=404, detail="no such artifact")
        if data.startswith(PNG_SIGNATURE):
            return Response(data, media_type="image/png")
        return Response(data, media_type="application/octet-stream", headers=attach)
    body = await store.get_text(job_id, path)
    if body is None:
        raise HTTPException(status_code=404, detail="no such artifact")
    media = "application/json" if meta.kind == "json" else "text/plain; charset=utf-8"
    return Response(body, media_type=media, headers=attach)


@router.post("/jobs/{ref}/actions", status_code=202)
async def post_action(
    request: Request,
    body: ActionRequest,
    principal: Annotated[Principal, Depends(require("submitter"))],
    ja: Annotated[JobAccess, Depends(job_access("viewer"))],
) -> dict[str, str]:
    need = ACTION_ROLE[body.action]
    if ROLE_RANK[ja.role] < ROLE_RANK[need]:
        raise HTTPException(status_code=403, detail=f"{need} role required for app '{ja.app_slug}'")
    d = _deps(request)
    if (
        body.action is HumanAction.CANCEL
        and ROLE_RANK[ja.role] < ROLE_RANK["approver"]
        and ja.job.requested_by != principal.email
    ):
        raise HTTPException(status_code=403, detail="a submitter may cancel only their own jobs")
    job_id = ja.job.id
    try:
        async with d.sessions.begin() as s:
            cid = await submit_command(
                s,
                job_id=job_id,
                action=body.action,
                note=body.note,
                requested_by=principal.email,
                head_sha=body.head_sha,
                amount_usd=body.amount_usd,
            )
    except CommandRefused as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    await d.events.emit(
        job_id,
        "human.action",
        {
            "action": body.action.value,
            "by": principal.email,
            "command_id": str(cid),
            "head_sha": body.head_sha,
            **({"amount_usd": str(body.amount_usd)} if body.amount_usd is not None else {}),
            "status": "requested",
        },
    )
    return {"command_id": str(cid)}


@router.post("/jobs/{ref}/triage-correction", status_code=202)
async def triage_correction(
    request: Request,
    body: dict[str, str | bool | int | float],
    principal: Annotated[Principal, Depends(require("approver"))],
    ja: Annotated[JobAccess, Depends(job_access("approver"))],
) -> dict[str, str]:
    if not body or {"by", "at"} & body.keys():
        raise HTTPException(status_code=422, detail="send {question: value}")
    job_id = ja.job.id
    d = _deps(request)
    at = datetime.now(UTC).isoformat()
    doc = {**body, "by": principal.email, "at": at}
    await d.artifacts.put(job_id, "triage-correction.json", json.dumps(doc), kind="json")
    await d.events.emit(
        job_id,
        "human.action",
        {"action": "correct_triage", "by": principal.email, "status": "requested"},
    )
    return {"status": "stored"}
