from __future__ import annotations

from swfactory.domain.types import DEFAULT_REVIEWERS, Stage
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid
from swfactory.graph.state import JobState


def make(deps: Deps):
    async def triage(state: JobState) -> dict:
        await enter_stage(deps, job_uuid(state), Stage.TRIAGE)
        return {"reviewers": list(DEFAULT_REVIEWERS)}  # M3.8 replaces it

    return triage
