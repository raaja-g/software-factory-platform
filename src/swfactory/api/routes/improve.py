"""Improvements: what the improver proposed, with its evidence, and a manual run-now.

Read-only apart from `POST /improvements/run`, which only queues work. Approval and merging
happen on the definition repo's pull request, never here."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from swfactory.api.auth import Principal, require
from swfactory.bench.report import BenchReport, ConfigSummary, build_report
from swfactory.db.tables import Improvement, Job
from swfactory.definition.store import NoActiveDefinition
from swfactory.domain.types import Role
from swfactory.improve.graph import start_cycle
from swfactory.improve.proposal import compare

if TYPE_CHECKING:
    from swfactory.api.app import ApiDeps

__all__ = ["router"]

router = APIRouter()
MAX_LIMIT = 200
MAX_CHANGES = 200


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = None


def _money(v: Any) -> str:
    return f"{Decimal(v or 0):.6f}"


def _rate(configs: tuple[ConfigSummary, ...]) -> float | None:
    rates = [c.hidden_pass_rate for c in configs if c.hidden_pass_rate is not None]
    return sum(rates) / len(rates) if rates else None


async def _report(s: Any, run_id: UUID | None) -> BenchReport | None:
    if run_id is None:
        return None
    from swfactory.api.routes.bench import load_run

    found = await load_run(s, run_id)
    return build_report(*found) if found is not None else None


async def _summary(s: Any, row: Improvement) -> tuple[dict[str, Any], Any]:
    before = await _report(s, row.bench_run_before)
    after = await _report(s, row.bench_run_after)
    result = "unmeasured"
    if before is not None and after is not None:
        try:
            role = Role(row.role_group)
        except ValueError:
            role = None
        result = compare(before.configs, after.configs, role=role).result
    out = {
        "id": str(row.id),
        "role_group": row.role_group,
        "trigger": row.trigger,
        "status": row.status,
        "branch": row.branch,
        "pr_url": row.pr_url,
        "base_sha": row.base_sha,
        "proposed_sha": row.proposed_sha,
        "result": result,
        "hidden_before": _rate(before.configs) if before else None,
        "hidden_after": _rate(after.configs) if after else None,
        "cost_usd": _money(row.cost_usd),
        "created_at": row.created_at.isoformat(),
    }
    return out, (before, after)


async def load_improvements(s: Any, limit: int) -> list[dict[str, Any]]:
    rows = await s.scalars(select(Improvement).order_by(Improvement.created_at.desc()).limit(limit))
    return [(await _summary(s, r))[0] for r in rows]


def _signals(evidence: Any) -> list[dict[str, str]]:
    items = evidence.get("signals") if isinstance(evidence, dict) else None
    keys = ("job_id", "job_ref", "role", "kind", "detail", "at")
    return [
        {k: str(d.get(k, "")) for k in keys}
        for d in (items if isinstance(items, list) else [])
        if isinstance(d, dict)
    ]


def _violations(evidence: Any) -> list[dict[str, str]]:
    items = evidence.get("violations") if isinstance(evidence, dict) else None
    return [
        {str(k): str(v) for k, v in d.items()}
        for d in (items if isinstance(items, list) else [])
        if isinstance(d, dict)
    ]


def _comparison(before: BenchReport | None, after: BenchReport | None) -> list[dict[str, Any]]:
    if before is None or after is None:
        return []
    by_after = {c.name: c for c in after.configs}
    out: list[dict[str, Any]] = []
    for b in before.configs:
        a = by_after.get(b.name)
        if a is None:
            continue
        out.append(
            {
                "config": b.name,
                "hidden_before": b.hidden_pass_rate,
                "hidden_after": a.hidden_pass_rate,
                "quality_before": b.quality,
                "quality_after": a.quality,
                "mean_cost_before": None if b.mean_cost_usd is None else _money(b.mean_cost_usd),
                "mean_cost_after": None if a.mean_cost_usd is None else _money(a.mean_cost_usd),
            }
        )
    return out


async def load_improvement(s: Any, artifacts: Any, iid: UUID) -> dict[str, Any] | None:
    row = await s.get(Improvement, iid)
    if row is None:
        return None
    out, (before, after) = await _summary(s, row)
    changes: list[dict[str, str]] = []
    if row.job_id is not None and artifacts is not None:
        try:
            raw = await artifacts.get_text(row.job_id, "changes.json")
            for d in (json.loads(raw) if raw else [])[:MAX_CHANGES]:
                changes.append({"path": str(d["path"]), "status": str(d["status"])})
        except Exception:  # an unreadable artifact must not hide the rest of the record
            changes = []
    job_cost = (
        await s.scalar(select(Job.cost_usd).where(Job.id == row.job_id)) if row.job_id else None
    )
    bench_before = before.cost_usd if before else Decimal(0)
    bench_after = after.cost_usd if after else Decimal(0)
    out["evidence"] = {
        "signals": _signals(row.evidence),
        "changes": changes,
        "comparison": _comparison(before, after),
        "violations": _violations(row.evidence),
        "costs": {
            "improver_usd": _money(job_cost),
            "bench_before_usd": _money(bench_before),
            "bench_after_usd": _money(bench_after),
            "total_usd": _money(row.cost_usd),
        },
    }
    out["error"] = row.error
    return out


@router.get("/improvements")
async def list_improvements(
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
) -> Any:
    deps: ApiDeps = request.app.state.deps
    async with deps.sessions() as s:
        return await load_improvements(s, limit)


@router.get("/improvements/{improvement_id}")
async def get_improvement(
    improvement_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
) -> Any:
    deps: ApiDeps = request.app.state.deps
    try:
        iid = UUID(improvement_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such improvement") from None
    async with deps.sessions() as s:
        found = await load_improvement(s, deps.artifacts, iid)
    if found is None:
        raise HTTPException(status_code=404, detail="no such improvement")
    return found


@router.post("/improvements/run")
async def run_improver(
    body: RunBody,
    request: Request,
    principal: Annotated[Principal, Depends(require("admin"))],
) -> dict[str, list[str]]:
    deps: ApiDeps = request.app.state.deps
    role: Role | None = None
    if body.role is not None:
        try:
            role = Role(body.role)
        except ValueError:
            raise HTTPException(
                status_code=422, detail=f"'{body.role[:40]}' is not a role"
            ) from None
    try:
        _, defn = await deps.definitions.active()
    except NoActiveDefinition:
        raise HTTPException(status_code=409, detail="no active definition") from None
    try:
        async with deps.sessions() as s, s.begin():
            ids = await start_cycle(s, defn, trigger="manual", role=role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except NoActiveDefinition:
        raise HTTPException(status_code=409, detail="no active definition") from None
    return {"ids": [str(i) for i in ids]}
