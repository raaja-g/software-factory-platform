"""Benchmark routes: turn a merged job into a benchmark task."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from swfactory.api.access import visible_apps
from swfactory.api.auth import Principal, require
from swfactory.api.routes import definition as definition_routes
from swfactory.bench.report import BenchReport, build_report
from swfactory.bench.runner import EstimateChanged, compute_estimate, start_bench_run
from swfactory.bench.tasks import (
    BenchTaskError,
    GitHubMergedChanges,
    LocalMergedChanges,
    MergedChanges,
    app_for_repo,
    task_from_job,
)
from swfactory.db.tables import BenchResult, BenchRun
from swfactory.definition.models import AppDef, Definition
from swfactory.definition.store import NoActiveDefinition
from swfactory.definition.writer import DefinitionWriteError
from swfactory.domain.types import FactoryMode

if TYPE_CHECKING:
    from swfactory.api.app import ApiDeps

router = APIRouter()
LOADED_TIMEOUT_S = 30.0


def _no_github(_repo: str) -> Any:
    raise RuntimeError("the API holds no GitHub credentials; add this task from the orchestrator")


class _ByKind:
    """Local apps read the shared bare repos; GitHub apps need a client factory."""

    def __init__(self, repos_dir: Path, github: Any = _no_github) -> None:
        self._local = LocalMergedChanges(repos_dir)
        self._github = GitHubMergedChanges(github)

    def _for(self, app: AppDef) -> MergedChanges:
        return self._github if app.repo_kind == "github" else self._local

    async def changed(self, app: AppDef, merge_sha: str) -> list[tuple[str, str]]:
        return await self._for(app).changed(app, merge_sha)

    async def read(self, app: AppDef, sha: str, path: str) -> str | None:
        return await self._for(app).read(app, sha, path)


def merged_changes(request: Request) -> MergedChanges:
    override = getattr(request.app.state, "merged_changes", None)
    if override is not None:
        return override
    return _ByKind(Path(request.app.state.deps.settings.repos_dir))


class AddTask(BaseModel):
    job: str
    suite: str
    name: str | None = None
    scorers: list[str] = Field(default_factory=list)
    check_command: str | None = None


@router.post("/bench/tasks", status_code=201)
async def add_task(
    body: AddTask,
    request: Request,
    principal: Annotated[Principal, Depends(require("admin"))],
) -> Any:
    deps: ApiDeps = request.app.state.deps
    if deps.writer is None:
        raise HTTPException(status_code=501, detail="no definition writer is configured")
    try:
        _, defn = await deps.definitions.active()
    except NoActiveDefinition:
        raise HTTPException(status_code=409, detail="no active definition") from None
    try:
        async with deps.sessions() as s:
            draft = await task_from_job(
                s,
                body.job,
                suite=body.suite,
                name=body.name,
                scorers=body.scorers,
                check_command=body.check_command,
                changes=merged_changes(request),
                artifacts=deps.artifacts,
                defn=defn,
            )
    except BenchTaskError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)[:300]) from None
    try:
        saved = await deps.writer.save_files(
            draft.files,
            message=f"bench: add {draft.name} from job {body.job}",
            author=principal.email,
        )
    except DefinitionWriteError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    save: dict[str, Any] = {
        "mode": getattr(saved, "mode", None),
        "sha": getattr(saved, "sha", None),
        "pr_url": getattr(saved, "pr_url", None),
        "active_sha": None,
    }
    out = {"suite": draft.suite, "task": draft.name, "save": save}
    sha = save["sha"]
    if deps.settings.mode is FactoryMode.PERSONAL and sha:
        after = await definition_routes.stream_position_now(deps)
        await definition_routes.notify_sync(deps, {"reason": "bench"})
        if await definition_routes.wait_loaded(
            deps, sha, after_id=after, timeout_s=LOADED_TIMEOUT_S
        ):
            save["active_sha"] = sha
        else:
            return JSONResponse(out, status_code=202)
    return out


class EstimateBody(BaseModel):
    suite: str
    configs: list[str] | None = None
    repetitions: int = Field(default=1, ge=1)


class StartRun(EstimateBody):
    budget_usd: Decimal | None = Field(default=None, gt=0)
    definition_sha: str | None = None
    accept_estimate_usd: Decimal


async def _definition_for(deps: ApiDeps, sha: str | None) -> tuple[str, Definition]:
    try:
        if sha is None:
            return await deps.definitions.active()
        found = await deps.definitions.get(sha)
    except NoActiveDefinition:
        raise HTTPException(status_code=409, detail="no active definition") from None
    if found is None:
        raise HTTPException(status_code=404, detail=f"no definition {sha}")
    return sha, found


@router.post("/bench/estimate")
async def estimate(
    body: EstimateBody,
    request: Request,
    _: Annotated[Principal, Depends(require("admin"))],
) -> Any:
    deps: ApiDeps = request.app.state.deps
    _sha, defn = await _definition_for(deps, None)
    try:
        async with deps.sessions() as s:
            sdef, chosen, est = await compute_estimate(
                s, defn, suite=body.suite, configs=body.configs, repetitions=body.repetitions
            )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {
        "suite": body.suite,
        "configs": [c.name for c in chosen],
        "repetitions": body.repetitions,
        "trials": est.trials,
        "estimate_usd": str(est.total_usd),
        "max_usd": str(est.max_usd),
        "budget_usd": str(sdef.budget_usd) if sdef.budget_usd is not None else None,
        "per_config": {k: str(v) for k, v in est.per_config.items()},
        "basis": dict(est.basis),
    }


@router.post("/bench/runs", status_code=202)
async def start_run(
    body: StartRun,
    request: Request,
    principal: Annotated[Principal, Depends(require("admin"))],
) -> Any:
    deps: ApiDeps = request.app.state.deps
    sha, defn = await _definition_for(deps, body.definition_sha)
    try:
        async with deps.sessions() as s:
            run_id = await start_bench_run(
                s,
                defn,
                suite=body.suite,
                configs=body.configs,
                repetitions=body.repetitions,
                budget_usd=body.budget_usd,
                definition_sha=sha,
                accept_estimate_usd=body.accept_estimate_usd,
                requested_by=principal.email,
            )
    except EstimateChanged as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"run_id": str(run_id), "status": "running"}


@router.post("/bench/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    request: Request,
    _: Annotated[Principal, Depends(require("admin"))],
) -> Any:
    deps: ApiDeps = request.app.state.deps
    try:
        rid = UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no bench run {run_id}") from None
    async with deps.sessions() as s, s.begin():
        row = await s.get(BenchRun, rid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no bench run {run_id}")
        if row.status != "running":
            raise HTTPException(status_code=409, detail=f"run is already {row.status}")
        await s.execute(update(BenchRun).where(BenchRun.id == rid).values(status="cancelled"))
    return {"run_id": run_id, "status": "cancelled"}


def _app_key(defn: Definition, repo: str) -> str | None:
    """The slug (key in factory.apps) of the app a task's repo names, or None."""
    try:
        app = app_for_repo(defn, repo)
    except KeyError:
        return None
    for key, a in defn.factory.apps.items():
        if a is app or a == app:
            return key
    return None


def _tasks_visible(
    defn: Definition, suite: str, tasks: Any, visible: frozenset[str] | None
) -> bool:
    if visible is None:
        return True
    sdef = defn.benchmarks.get(suite)
    if sdef is None:
        return False
    for name in tasks:
        t = sdef.tasks.get(name)
        if t is None or _app_key(defn, t.repo) not in visible:
            return False
    return True


def _row(r: BenchRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "suite": r.suite,
        "status": r.status,
        "definition_sha": r.definition_sha,
        "budget_usd": r.budget_usd,
        "cost_usd": r.cost_usd,
        "estimate_usd": r.estimate_usd,
        "repetitions": r.repetitions,
        "requested_by": r.requested_by,
        "created_at": r.created_at,
        "finished_at": r.finished_at,
    }


def _result_row(r: BenchResult) -> dict[str, Any]:
    return {
        "configuration": r.configuration,
        "task": r.task,
        "status": r.status,
        "hidden_passed": r.hidden_passed,
        "judge_pass": r.judge_pass,
        "judge_pass_uncalibrated": r.judge_pass_uncalibrated,
        "rounds": r.rounds,
        "cost_usd": r.cost_usd,
        "duration_s": r.duration_s,
        "repetition": r.repetition,
        "hidden_detail": r.hidden_detail,
    }


async def load_runs(s: Any) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Every bench run, newest first, with its results."""
    runs = list(await s.scalars(select(BenchRun).order_by(BenchRun.created_at.desc())))
    by_run: dict[Any, list[dict[str, Any]]] = {r.id: [] for r in runs}
    if runs:
        rows = await s.scalars(select(BenchResult).where(BenchResult.run_id.in_(list(by_run))))
        for res in rows:
            by_run[res.run_id].append(_result_row(res))
    return [(_row(r), by_run[r.id]) for r in runs]


async def load_run(s: Any, run_id: Any) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    run = await s.get(BenchRun, run_id)
    if run is None:
        return None
    rows = await s.scalars(select(BenchResult).where(BenchResult.run_id == run_id))
    return _row(run), [_result_row(r) for r in rows]


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, UUID)):
        return v.isoformat() if isinstance(v, datetime) else str(v)
    return v


def report_json(report: BenchReport, results: list[dict[str, Any]]) -> dict[str, Any]:
    out = _jsonable(asdict(report))
    out["id"] = str(report.run_id)
    out["invalid_reasons"] = {
        t: next(
            (r["hidden_detail"] for r in results if r["task"] == t and r["hidden_detail"]), None
        )
        for t in report.invalid_tasks
    }
    return out


async def _visibility(
    request: Request, principal: Principal
) -> tuple[Definition, frozenset[str] | None]:
    deps: ApiDeps = request.app.state.deps
    try:
        _, defn = await deps.definitions.active()
    except NoActiveDefinition:
        raise HTTPException(status_code=409, detail="no active definition") from None
    return defn, visible_apps(defn, principal, mode=request.app.state.mode)


@router.get("/bench/suites")
async def list_suites(
    request: Request, principal: Annotated[Principal, Depends(require("viewer"))]
) -> Any:
    defn, visible = await _visibility(request, principal)
    return [
        {
            "name": name,
            "description": sdef.description,
            "tasks": len(sdef.tasks),
            "configurations": [c.name for c in sdef.configurations],
            "budget_usd": str(sdef.budget_usd),
        }
        for name, sdef in sorted(defn.benchmarks.items())
        if _tasks_visible(defn, name, sdef.tasks, visible)
    ]


@router.get("/bench/runs")
async def list_runs(
    request: Request, principal: Annotated[Principal, Depends(require("viewer"))]
) -> Any:
    deps: ApiDeps = request.app.state.deps
    defn, visible = await _visibility(request, principal)
    async with deps.sessions() as s:
        found = await load_runs(s)
    return [
        _jsonable({"id": run["id"], **{k: v for k, v in run.items() if k != "id"}})
        for run, results in found
        if _tasks_visible(defn, run["suite"], {r["task"] for r in results}, visible)
    ]


@router.get("/bench/runs/{run_id}")
async def get_run(
    run_id: str, request: Request, principal: Annotated[Principal, Depends(require("viewer"))]
) -> Any:
    deps: ApiDeps = request.app.state.deps
    defn, visible = await _visibility(request, principal)
    try:
        rid = UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no bench run {run_id}") from None
    async with deps.sessions() as s:
        found = await load_run(s, rid)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no bench run {run_id}")
    run, results = found
    if not _tasks_visible(defn, run["suite"], {r["task"] for r in results}, visible):
        raise HTTPException(status_code=404, detail=f"no bench run {run_id}")
    return report_json(build_report(run, results), results)
