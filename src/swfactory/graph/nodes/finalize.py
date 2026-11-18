from __future__ import annotations

from datetime import UTC, datetime

import structlog

from swfactory.domain.types import TERMINAL_STAGES, Stage
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid
from swfactory.graph.state import JobState
from swfactory.graph.steps import keep_job_branch

log = structlog.get_logger(__name__)


def make(deps: Deps):
    async def finalize(state: JobState) -> dict:
        job_id = job_uuid(state)
        stage = Stage(state.get("outcome") or Stage.CLOSED.value)
        fields = {"finished_at": datetime.now(UTC)} if stage in TERMINAL_STAGES else {}
        await enter_stage(deps, job_id, stage, **fields)  # the stage first, then cleanup
        try:
            await keep_job_branch(deps, job_id)  # never raises
        except Exception as exc:
            log.warning("finalize.keep_branch_failed", error=str(exc))
        try:
            await deps.sandboxes.destroy(job_id)
        except Exception as exc:  # the reaper removes leftovers (M4.4)
            log.warning("finalize.cleanup_failed", error=str(exc))
        return {"outcome": stage.value}

    return finalize
