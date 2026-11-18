from __future__ import annotations

from swfactory.domain.types import Stage
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid
from swfactory.graph.state import JobState
from swfactory.graph.steps import _runner, pinned_definition


def make(deps: Deps):
    async def prepare(state: JobState) -> dict:
        job_id = job_uuid(state)
        job, defn, app = await pinned_definition(deps, job_id)
        box = await deps.sandboxes.ensure(job_id, _runner(defn, app))
        await deps.git.prepare(job, app, box)
        if await deps.artifacts.get_text(job_id, "feature.md") is None:
            await deps.artifacts.put(job_id, "feature.md", f"# {job.title}\n\n{job.feature}\n")
        await enter_stage(deps, job_id, Stage.TRIAGE)
        return {}

    return prepare
