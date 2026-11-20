"""API-to-worker score requests: the API inserts a row, a worker loop runs the judges."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import or_, select

from swfactory.db.tables import ScoreRequest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from swfactory.graph.deps import Deps
    from swfactory.scoring.service import ScoreSummary

log = structlog.get_logger()

# SCORE_RUN_TIMEOUT_S (1800) plus five minutes of slack
STALE_RUNNING_S = 35 * 60
RETRIED = "retried"


def summary_json(summary: ScoreSummary) -> dict[str, Any]:
    return {
        "job_id": str(summary.job_id),
        "quality": summary.quality,
        "rows": summary.rows,
        "judges_run": summary.judges_run,
        "judges_skipped": list(summary.judges_skipped),
        "errors": list(summary.errors),
    }


async def create_score_request(
    s: AsyncSession,
    *,
    job_id: UUID,
    scorers: Sequence[str] | None,
    force: bool,
    requested_by: str,
) -> UUID:
    row = ScoreRequest(
        job_id=job_id,
        scorers=None if scorers is None else list(scorers),
        force=force,
        requested_by=requested_by,
    )
    s.add(row)
    await s.flush()
    return row.id


async def get_score_request(s: AsyncSession, request_id: UUID) -> ScoreRequest | None:
    return await s.get(ScoreRequest, request_id)


async def _claim(s: AsyncSession, now: datetime) -> ScoreRequest | None:
    stale = now - timedelta(seconds=STALE_RUNNING_S)
    return await s.scalar(
        select(ScoreRequest)
        .where(
            or_(
                ScoreRequest.status == "pending",
                (ScoreRequest.status == "running") & (ScoreRequest.updated_at <= stale),
            )
        )
        .order_by(ScoreRequest.created_at, ScoreRequest.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )


async def score_request_tick(deps: Deps, worker_id: str, *, now: datetime | None = None) -> bool:
    """Claim and run one request; True when one was handled. Never raises for a bad request."""
    from swfactory.graph import build

    now = now or datetime.now(UTC)
    async with deps.sessions.begin() as s:  # short claim transaction, no lock held while scoring
        row = await _claim(s, now)
        if row is None:
            return False
        if row.status == "running" and row.detail == RETRIED:
            row.status = "failed"
            row.detail = "the worker stopped twice while scoring (retried once)"
            row.updated_at = now
            return True
        if row.status == "running":
            row.detail = RETRIED  # one more attempt for a row stuck in running
        row.status = "running"
        row.updated_at = now
        request_id, job_id, force, only = row.id, row.job_id, row.force, row.scorers
    log.info("score_request.claimed", request_id=str(request_id), worker_id=worker_id)
    status, summary, detail = "done", None, None
    try:
        result = await asyncio.wait_for(
            deps.scoring.score_job(deps, job_id, force=force, only=only),
            timeout=build.SCORE_RUN_TIMEOUT_S,
        )
        summary = summary_json(result)
    except TimeoutError:
        status, detail = "failed", f"scoring timed out after {build.SCORE_RUN_TIMEOUT_S} s"
    except Exception as exc:
        status, detail = "failed", f"{type(exc).__name__}: {exc}"
    async with deps.sessions.begin() as s:
        done = await s.get(ScoreRequest, request_id, populate_existing=True)
        if done is not None:
            done.status = status
            done.summary = summary
            done.detail = detail
            done.updated_at = datetime.now(UTC)
    return True
