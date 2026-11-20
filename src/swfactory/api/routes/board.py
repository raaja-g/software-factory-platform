"""The board: jobs grouped into columns, with the fields the dashboard needs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.api.access import visible_apps
from swfactory.api.auth import Principal, require
from swfactory.backlogs import format_backlog_ref
from swfactory.commands import format_job_ref, retry_offered
from swfactory.db.repo import _iso, board, latest_waiting
from swfactory.db.tables import AgentRun, App, Backlog, Job, Score
from swfactory.definition.store import NoActiveDefinition, load_active
from swfactory.domain.types import Stage
from swfactory.events import stream_position
from swfactory.graph.build import GRAPH_VERSION

V1_KEYS = frozenset(
    {
        "id", "title", "branch", "stage", "round", "reviews", "history", "decision", "files",
        "updated",
    }
)  # fmt: skip

_COLUMNS: dict[Stage, str] = {
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


def column_for(stage: Stage) -> str:
    return _COLUMNS[stage]


def board_job(row: Mapping[str, Any]) -> dict[str, Any]:
    """One board entry from a flat row: v1's keys plus the dashboard's additions."""
    stage = Stage(row["stage"])
    jid = f"{row['seq']:03d}-{row['slug']}"
    waiting = row.get("waiting") or {}
    offered = retry_offered(waiting)
    if stage is Stage.FAILED:
        offered = bool(row.get("graph_supported", True))
    return {
        "id": jid,
        "job_id": str(row["job_id"]),
        "ref": format_job_ref(row["app"], row["seq"]),
        "app": row["app"],
        "title": row["title"],
        "branch": row.get("branch") or f"factory/{jid}",
        "stage": stage.value,
        "column": column_for(stage),
        "round": row.get("round", 0),
        "reviews": row.get("reviews", {}),
        "history": row.get("history", []),
        "decision": row.get("decision"),
        "files": row.get("files", []),
        "updated": row["updated"],
        "gate_reason": row.get("gate_reason"),
        "retry_offered": offered,
        "head_sha": waiting.get("head_sha"),
        "stuck": bool(row.get("stuck", False)),
        "cost_usd": row.get("cost_usd", "0.000000"),
        "budget_usd": row.get("budget_usd"),
        "models": row.get("models", {}),
        "fallback": bool(row.get("fallback", False)),
        "quality": row.get("quality") if column_for(stage) == "done" else None,
        "merge_policy": row.get("merge_policy"),
        "pr_url": row.get("pr_url"),
        "kind": row.get("kind", "normal"),
        "backlog_ref": row.get("backlog_ref"),
        "backlog_pos": row.get("backlog_pos"),
    }


_DONE_STAGES = frozenset(s.value for s, c in _COLUMNS.items() if c == "done")


async def _quality(s: AsyncSession, ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """The badge and judge tally for done jobs: ``obj.quality`` plus each judge's latest row."""
    if not ids:
        return {}
    rows = (
        await s.scalars(
            select(Score)
            .where(
                Score.job_id.in_(ids),
                or_(Score.scorer == "obj.quality", Score.judge_model.is_not(None)),
            )
            .order_by(Score.created_at, Score.id)
        )
    ).all()
    badge: dict[UUID, str] = {}
    judges: dict[UUID, dict[tuple[str, str], bool | None]] = defaultdict(dict)
    for r in rows:
        if r.scorer == "obj.quality":
            badge[r.job_id] = r.label
        elif r.judge_model is not None:
            judges[r.job_id][(r.scorer, r.judge_model)] = r.passed
    return {
        jid: {
            "badge": b,
            "judges_passed": sum(1 for p in judges[jid].values() if p),
            "judges_total": sum(1 for p in judges[jid].values() if p is not None),
        }
        for jid, b in badge.items()
    }


async def build_board(
    s: AsyncSession,
    *,
    app_slug: str | None,
    done_limit: int = 50,
    app_slugs: Collection[str] | None = None,
) -> dict[str, Any]:
    try:  # one snapshot for the jobs and the stream position
        await s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    except InvalidRequestError:
        pass
    last_event_id = await stream_position(s)
    base = (await board(s, app_slug=app_slug, app_slugs=app_slugs))["jobs"]
    ids = [UUID(b["job_id"]) for b in base]
    jobs: dict[UUID, Job] = {}
    runs: dict[UUID, dict[str, AgentRun]] = defaultdict(dict)
    if ids:
        for j in (await s.scalars(select(Job).where(Job.id.in_(ids)))).all():
            jobs[j.id] = j
        for r in (
            await s.scalars(
                select(AgentRun)
                .where(AgentRun.job_id.in_(ids))
                .order_by(AgentRun.started_at, AgentRun.attempt)
            )
        ).all():
            runs[r.job_id][r.role] = r
    latest_kind: dict[UUID, str] = {}
    if ids:
        for jid, kind in (
            await s.execute(
                text(
                    "SELECT DISTINCT ON (job_id) job_id, kind FROM app.job_events "
                    "WHERE job_id = ANY(:ids) ORDER BY job_id, id DESC"
                ),
                {"ids": ids},
            )
        ).all():
            latest_kind[jid] = kind
    bl_seq: dict[UUID, int] = {}
    bl_ids = {j.backlog_id for j in jobs.values() if j.backlog_id is not None}
    if bl_ids:
        bl_seq = {
            i: n
            for i, n in (
                await s.execute(select(Backlog.id, Backlog.seq).where(Backlog.id.in_(bl_ids)))
            ).all()
        }
    quality = await _quality(s, [UUID(b["job_id"]) for b in base if b["stage"] in _DONE_STAGES])
    found = await load_active(s)
    defn = None if found is None else found[1]
    out: list[dict[str, Any]] = []
    for b in base:
        jid = UUID(b["job_id"])
        j = jobs[jid]
        waiting = None
        if j.stage == Stage.NEEDS_HUMAN.value:
            w = await latest_waiting(s, jid)
            waiting = None if w is None else w[1]
        by_role = runs.get(jid, {})
        models = {
            role: {
                "alias": r.model_alias,
                "engine": r.engine,
                "mode": r.selection_mode,
                "fallback": r.fallback_detected,
            }
            for role, r in by_role.items()
        }
        app_def = None if defn is None else defn.factory.apps.get(b["app"])
        policy = None if app_def is None or app_def.merge_policy is None else app_def.merge_policy
        row = {
            **b,
            "seq": j.seq,
            "slug": j.slug,
            "waiting": waiting,
            "stuck": latest_kind.get(jid) == "job.stuck",
            "gate_reason": j.gate_reason,
            "graph_supported": j.graph_version is None or j.graph_version == str(GRAPH_VERSION),
            "cost_usd": f"{j.cost_usd:.6f}",
            "budget_usd": f"{j.budget_usd:.2f}",
            "models": models,
            "fallback": any(m["fallback"] for m in models.values()),
            "merge_policy": None if policy is None else policy.value,
            "pr_url": j.pr_url,
            "kind": j.kind,
            "backlog_ref": None
            if j.backlog_id is None or j.backlog_id not in bl_seq
            else format_backlog_ref(b["app"], bl_seq[j.backlog_id]),
            "backlog_pos": j.backlog_pos,
            "quality": quality.get(jid),
        }
        out.append(board_job(row))
    done = [j for j in out if j["column"] == "done"][:done_limit]
    kept = [j for j in out if j["column"] != "done"] + done
    q = select(App.slug).where(App.archived_at.is_(None))
    if app_slugs is not None:
        q = q.where(App.slug.in_(list(app_slugs)))
    apps = sorted((await s.scalars(q)).all())
    return {
        "jobs": kept,
        "apps": apps,
        "last_event_id": last_event_id,
        "generated_at": _iso(datetime.now(UTC)),
    }


router = APIRouter()


@router.get("/board")
async def get_board(
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
    app: str | None = None,
) -> dict[str, Any]:
    deps = request.app.state.deps
    visible = None
    try:
        _, defn = await deps.definitions.active()
        visible = visible_apps(defn, principal, mode=request.app.state.mode)
    except NoActiveDefinition:
        pass
    async with deps.sessions() as s:
        return await build_board(s, app_slug=app, app_slugs=visible)
