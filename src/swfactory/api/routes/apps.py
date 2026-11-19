"""GET /api/apps: definition apps joined with their rows, job counts and today's spend."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.api.access import visible_apps
from swfactory.api.auth import Principal, require
from swfactory.db.tables import AgentRun, App, Job
from swfactory.domain.types import Stage

if TYPE_CHECKING:
    from swfactory.api.app import ApiDeps

router = APIRouter()

COLUMNS = ("queued", "triage", "spec", "build", "review", "approve", "needs_human", "done")
_COLUMN = {
    Stage.QUEUED: "queued",
    Stage.TRIAGE: "triage",
    Stage.SPEC: "spec",
    Stage.BUILD: "build",
    Stage.VERIFY: "build",
    Stage.REVIEW: "review",
    Stage.APPROVE: "approve",
    Stage.DELIVERING: "approve",
    Stage.NEEDS_HUMAN: "needs_human",
    Stage.FAILED: "needs_human",
    Stage.DELIVERED: "done",
    Stage.MERGED: "done",
    Stage.CLOSED: "done",
    Stage.CANCELLED: "done",
}


def stage_column(stage: str) -> str:
    return _COLUMN[Stage(stage)]


async def app_stats(s: AsyncSession, *, now: datetime) -> dict[str, tuple[dict[str, int], Decimal]]:
    """slug -> (job count per column, spend since midnight UTC), for apps with rows."""
    counts: dict[str, dict[str, int]] = {}
    rows = await s.execute(
        select(App.slug, Job.stage, func.count())
        .join(Job, Job.app_id == App.id)
        .group_by(App.slug, Job.stage)
    )
    for slug, stage, n in rows.all():
        col = counts.setdefault(slug, dict.fromkeys(COLUMNS, 0))
        col[stage_column(stage)] += int(n)
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    spend = await s.execute(
        select(App.slug, func.coalesce(func.sum(AgentRun.cost_usd), 0))
        .join(Job, Job.app_id == App.id)
        .join(AgentRun, AgentRun.job_id == Job.id)
        .where(AgentRun.started_at >= midnight)
        .group_by(App.slug)
    )
    cost = {slug: Decimal(total or 0) for slug, total in spend.all()}
    out: dict[str, tuple[dict[str, int], Decimal]] = {
        slug: (c, cost.get(slug, Decimal(0))) for slug, c in counts.items()
    }
    for slug, t in cost.items():
        out.setdefault(slug, (dict.fromkeys(COLUMNS, 0), t))
    return out


@router.get("/apps")
async def list_apps(
    request: Request, principal: Annotated[Principal, Depends(require("viewer"))]
) -> list[dict[str, Any]]:
    deps: ApiDeps = request.app.state.deps
    _, defn = await deps.definitions.active()
    visible = visible_apps(defn, principal, mode=request.app.state.mode)
    async with deps.sessions() as s:
        archived = set(
            (await s.scalars(select(App.slug).where(App.archived_at.is_not(None)))).all()
        )
        stats = await app_stats(s, now=datetime.now(UTC))
    out: list[dict[str, Any]] = []
    for slug, a in defn.factory.apps.items():
        if slug in archived or (visible is not None and slug not in visible):
            continue
        counts, cost = stats.get(slug, (dict.fromkeys(COLUMNS, 0), Decimal(0)))
        max_rounds = a.max_rounds if a.max_rounds is not None else defn.factory.defaults.max_rounds
        budget = a.budget_usd if a.budget_usd is not None else defn.factory.budgets.job_usd
        out.append(
            {
                "slug": slug,
                "repo_kind": a.repo_kind,
                "default_branch": a.default_branch,
                "profile": a.profile or defn.factory.defaults.profile,
                "merge_policy": a.merge_policy.value if a.merge_policy else None,
                "max_rounds": max_rounds,
                "budget_usd": str(budget),
                "counts": counts,
                "cost_today_usd": str(cost),
            }
        )
    return out
