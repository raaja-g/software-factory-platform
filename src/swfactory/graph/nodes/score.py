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
        return {}

    return score
