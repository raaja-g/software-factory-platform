from __future__ import annotations

from uuid import UUID

import structlog

from swfactory.graph.deps import Deps
from swfactory.graph.state import JobState

log = structlog.get_logger()


def make(deps: Deps):
    async def score(state: JobState) -> dict:
        job_id = str(state.get("job_id"))
        try:
            await deps.scoring.score_job(deps, UUID(job_id))
        except Exception as exc:  # scoring never blocks delivery (L13)
            log.warning("score.failed", job_id=job_id, error=repr(exc))
        try:  # outcome labels for the Laya router never block or fail a job (L13)
            from swfactory.laya.export import label_job_outcomes

            async with deps.sessions() as s, s.begin():
                await label_job_outcomes(s, UUID(job_id))
        except Exception as exc:
            log.warning("laya.label_failed", job_id=job_id, error=repr(exc))
        return {}

    return score
