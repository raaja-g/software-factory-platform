from __future__ import annotations

from uuid import UUID

from swfactory.db.tables import Backlog
from swfactory.domain.types import Stage
from swfactory.graph.app_context import render_app_md, render_feature_md, stack_from_agents_md
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid
from swfactory.graph.state import JobState
from swfactory.graph.steps import _runner, pinned_definition
from swfactory.sandbox.paths import REPO


async def _requirements_of_backlog(deps: Deps, backlog_id: UUID) -> str | None:
    async with deps.sessions() as s:
        bl = await s.get(Backlog, backlog_id)
    if bl is None or bl.requirements_job_id is None:
        return None
    return await deps.artifacts.get_text(bl.requirements_job_id, "requirements.md")


def make(deps: Deps):
    async def prepare(state: JobState) -> dict:
        job_id = job_uuid(state)
        job, defn, app = await pinned_definition(deps, job_id)
        box = await deps.sandboxes.ensure(job_id, _runner(defn, app))
        await deps.git.prepare(job, app, box)
        if job.backlog_id is not None and job.backlog_pos is not None:
            reqs = await _requirements_of_backlog(deps, job.backlog_id)
            if reqs is not None:
                await deps.artifacts.put(job_id, "requirements.md", reqs)
        slug = next(k for k, v in defn.factory.apps.items() if v is app)
        stack = stack_from_agents_md(await box.read_text(f"{REPO}/AGENTS.md"))
        await deps.artifacts.put(job_id, "feature.md", render_feature_md(job.title, job.feature))
        await deps.artifacts.put(job_id, "app.md", render_app_md(slug, app, stack=stack))
        await enter_stage(deps, job_id, Stage.TRIAGE)
        return {}

    return prepare
