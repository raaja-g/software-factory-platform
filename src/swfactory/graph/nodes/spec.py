from __future__ import annotations

from langgraph.types import Command

from swfactory.domain.types import Role, RunStatus, Stage, ToolPolicy
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid, merged
from swfactory.graph.routing import budget_update, failure_update, route_or_gate
from swfactory.graph.state import JobState
from swfactory.graph.steps import run_agent_step
from swfactory.sandbox.paths import REPO


def make(deps: Deps):
    async def spec(state: JobState) -> Command:
        job_id = job_uuid(state)
        await enter_stage(deps, job_id, Stage.SPEC)
        out = await run_agent_step(
            deps,
            job_id=job_id,
            role=Role.SPEC_WRITER,
            round=state.get("round", 1),
            inputs=["feature.md", "requirements.md", "triage.md", "app.md"],
            output_name="spec.md",
            workdir=REPO,
            tool_policy=ToolPolicy.READ_ONLY,
        )
        update: dict = {}
        if out.status == RunStatus.BUDGET:
            update = budget_update("spec")
        elif out.status != RunStatus.OK:
            update = failure_update("spec")
        return Command(goto=route_or_gate(merged(state, update), "build"), update=update)

    return spec
