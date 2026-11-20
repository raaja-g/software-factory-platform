"""Jobs: listing, intake, detail, runs, events, artifacts and human actions."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text

from swfactory.api.access import JobAccess, app_role, authorize_job, job_access, visible_apps
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
from swfactory.db.repo import (
    calibration_table,
    job_judge_labels,
    job_judge_scores,
    latest_waiting,
    list_jobs,
    put_judge_label,
    resolve_job_ref,
)
from swfactory.db.tables import AgentRun, App, Backlog, Job, JobEvent, Score
from swfactory.definition.store import NoActiveDefinition
from swfactory.domain.types import HumanAction, Stage
from swfactory.github.followup import start_follow_up
from swfactory.graph.steps import pinned_definition
from swfactory.queue import UnknownApp, enqueue_job
from swfactory.scoring.calibration import calibration_tag
from swfactory.scoring.judge import rubric_sha
from swfactory.scoring.requests import create_score_request, get_score_request

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


class FollowUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def _not_blank(self) -> FollowUpRequest:
        if not self.note.strip():
            raise ValueError("note must not be blank")
        return self


@router.post("/jobs/{ref}/follow-up", status_code=201)
async def post_follow_up(
    request: Request,
    body: FollowUpRequest,
    principal: Annotated[Principal, Depends(require("submitter"))],
    ja: Annotated[JobAccess, Depends(job_access("approver"))],
) -> dict[str, str]:
    d = _deps(request)
    try:
        async with d.sessions.begin() as s:
            new = await start_follow_up(
                s, job_id=ja.job.id, note=body.note, requested_by=principal.email
            )
            out = {"job_id": str(new.id), "ref": format_job_ref(ja.app_slug, new.seq)}
    except CommandRefused as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return out


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


class LabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str


async def _active(request: Request) -> Any:
    try:
        _, defn = await _deps(request).definitions.active()
    except NoActiveDefinition:
        raise HTTPException(status_code=503, detail="no active definition") from None
    return defn


def _judge_view(r: Any, shown: bool, cal: Any) -> dict[str, Any]:
    return {
        "scorer": r.scorer,
        "judge_model": r.judge_model,
        "label": r.label if shown else None,
        "reason": r.reason if shown else None,
        "score": str(r.score) if shown and r.score is not None else None,
        "passed": r.passed if shown else None,
        "hidden": not shown,
        "calibrated": bool(cal and cal.calibrated),
        "calibration": calibration_tag(cal),
        "n": 0 if cal is None else cal.n,
        "kappa": None if cal is None else cal.kappa,
    }


@router.get("/jobs/{ref}/judges")
async def get_job_scores(
    request: Request, ja: Annotated[JobAccess, Depends(job_access("viewer"))]
) -> dict[str, Any]:
    """Judge rows for the job; a row's label and reason stay hidden until you label that scorer."""
    defn = await _active(request)
    async with _deps(request).sessions() as s:
        rows = await job_judge_scores(s, ja.job.id)
        labels = {
            (lb.scorer, lb.rubric_sha): lb.label for lb in await job_judge_labels(s, ja.job.id)
        }
        table = await calibration_table(s, defn.scorers)
    latest: dict[tuple[str, str | None, str | None], Any] = {}
    for r in rows:
        latest[(r.scorer, r.judge_model, r.judge_sha)] = r
    out: list[dict[str, Any]] = []
    for r in latest.values():
        sd = defn.scorers.get(r.scorer)
        mine = labels.get((r.scorer, r.rubric_sha))
        cal = table.get((r.scorer, r.judge_model or "", r.rubric_sha or "", r.judge_sha or ""))
        row = _judge_view(r, mine is not None, cal)
        row["your_label"] = mine
        row["labels"] = [] if sd is None else [lb.value for lb in sd.labels]
        out.append(row)
    return {"scores": out}


@router.put("/jobs/{ref}/scores/{scorer}/label")
async def put_label(
    request: Request,
    scorer: str,
    body: LabelRequest,
    principal: Annotated[Principal, Depends(require("approver"))],
    ja: Annotated[JobAccess, Depends(job_access("approver"))],
) -> dict[str, Any]:
    defn = await _active(request)
    sd = defn.scorers.get(scorer)
    if sd is None:
        raise HTTPException(status_code=404, detail=f"unknown scorer '{scorer}'")
    allowed = [lb.value for lb in sd.labels]
    if body.label not in allowed:
        raise HTTPException(status_code=422, detail=f"label must be one of {', '.join(allowed)}")
    sha = rubric_sha(sd)
    async with _deps(request).sessions.begin() as s:
        await put_judge_label(s, ja.job.id, scorer, body.label, sha, principal.email)
        rows = [r for r in await job_judge_scores(s, ja.job.id) if r.scorer == scorer]
        table = await calibration_table(s, defn.scorers)
    latest = {(r.judge_model, r.judge_sha): r for r in rows}
    judges = [
        _judge_view(
            r, True, table.get((scorer, r.judge_model or "", r.rubric_sha or "", r.judge_sha or ""))
        )
        for r in latest.values()
    ]
    return {"stored": True, "label": body.label, "judges": judges}


@router.get("/scorers/calibration")
async def get_calibration(
    request: Request, _: Annotated[Principal, Depends(require("viewer"))]
) -> list[dict[str, Any]]:
    defn = await _active(request)
    async with _deps(request).sessions() as s:
        table = await calibration_table(s, defn.scorers)
    return [
        {
            "scorer": c.scorer,
            "judge_model": c.judge_model,
            "rubric_sha": c.rubric_sha,
            "judge_sha": c.judge_sha,
            "n": c.n,
            "human_pass": c.human_pass,
            "human_fail": c.human_fail,
            "kappa": c.kappa,
            "calibrated": c.calibrated,
            "tag": calibration_tag(c),
        }
        for c in table.values()
    ]


def _score_row(r: Score) -> dict[str, Any]:
    return {
        "scorer": r.scorer,
        "label": r.label,
        "score": None if r.score is None else str(r.score),
        "passed": r.passed,
        "reason": r.reason,
        "judge_model": r.judge_model,
        "cost_usd": f"{r.cost_usd or 0:.6f}",
        "run_id": None if r.run_id is None else str(r.run_id),
        "created_at": r.created_at.astimezone(UTC).isoformat(),
    }


async def _scores(request: Request, job_id: Any) -> list[dict[str, Any]]:
    async with _deps(request).sessions() as s:
        rows = (
            await s.scalars(
                select(Score).where(Score.job_id == job_id).order_by(Score.scorer, Score.created_at)
            )
        ).all()
    return [_score_row(r) for r in rows]


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False
    scorers: list[str] | None = None


@router.get("/jobs/{ref}/scores")
async def get_scores(
    request: Request, ja: Annotated[JobAccess, Depends(job_access("viewer"))]
) -> list[dict[str, Any]]:
    return await _scores(request, ja.job.id)


async def _pending_judges(d: Any, job_id: Any, only: list[str] | None) -> list[str]:
    """Scorers of the job's pinned definition that a worker would run, subject to `only`."""
    try:
        _, defn, _ = await pinned_definition(d, job_id)
    except Exception:
        return []
    return [n for n in sorted(defn.scorers) if only is None or n in only]


@router.post("/jobs/{ref}/score")
async def post_score(
    request: Request,
    body: ScoreRequest,
    principal: Annotated[Principal, Depends(require("admin"))],
    ja: Annotated[JobAccess, Depends(job_access("admin"))],
) -> JSONResponse:
    """Rescore the objective rows here; judges need agents, so they go to a worker request."""
    from swfactory.scoring.service import ScoringService

    d = _deps(request)
    svc = ScoringService(d.sessions, d.events, d.definitions, d.catalog)
    summary = await svc.score_job(d, ja.job.id, force=body.force, only=body.scorers)
    pending = await _pending_judges(d, ja.job.id, body.scorers)
    out: dict[str, Any] = {
        "summary": {
            "quality": summary.quality,
            "rows": summary.rows,
            "judges_run": summary.judges_run,
            "judges_skipped": [
                line for line in summary.judges_skipped if not line.endswith(_ON_WORKER)
            ],
            "judges_pending": pending,
            "errors": list(summary.errors),
        },
    }
    status = 200
    if pending:
        async with d.sessions.begin() as s:
            rid = await create_score_request(
                s,
                job_id=ja.job.id,
                scorers=body.scorers,
                force=body.force,
                requested_by=principal.email,
            )
        out["request_id"] = str(rid)
        status = 202
    out["scores"] = await _scores(request, ja.job.id)
    return JSONResponse(out, status_code=status)


_ON_WORKER = ": judges run on the worker"


@router.get("/score-requests/{request_id}")
async def get_score_request_status(
    request: Request,
    request_id: str,
    principal: Annotated[Principal, Depends(require("viewer"))],
) -> dict[str, Any]:
    try:
        key = UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="score request not found") from None
    async with _deps(request).sessions() as s:
        row = await get_score_request(s, key)
        job = None if row is None else await resolve_job_ref(s, str(row.job_id))
    if row is None or job is None:
        raise HTTPException(status_code=404, detail="score request not found")
    await authorize_job(request, principal, job, "viewer")
    return {"status": row.status, "summary": row.summary, "detail": row.detail}
