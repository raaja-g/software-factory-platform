from __future__ import annotations

from langgraph.types import Command

from swfactory.domain.types import Role, RunStatus, Stage, ToolPolicy
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import build_inputs, enter_stage, job_uuid, merged
from swfactory.graph.routing import budget_update, failure_update, route_or_gate
from swfactory.graph.state import JobState
from swfactory.graph.steps import pinned_definition, run_agent_step
from swfactory.models.profiles import resolve_profile
from swfactory.sandbox.paths import REPO


def make(deps: Deps):
    async def build(state: JobState) -> Command:
        job_id = job_uuid(state)
        job, defn, app = await pinned_definition(deps, job_id)
        rnd = state.get("round", 1)
        await enter_stage(
            deps, job_id, Stage.BUILD, round=rnd, max_rounds=state.get("max_rounds", 2)
        )
        profile, _ = resolve_profile(
            defn,
            app,
            override=job.profile_name,
            triage_profile=(job.triage or {}).get("profile_name"),
        )
        resume = profile.continuity == "resume" and (rnd > 1 or state.get("fix_attempts", 0) > 0)
        out = await run_agent_step(
            deps,
            job_id=job_id,
            role=Role.BUILDER,
            round=rnd,
            inputs=build_inputs(state),
            output_name="build.md",
            workdir=REPO,
            tool_policy=ToolPolicy.WRITE,
            resume=resume,
            occurrence=state.get("fix_attempts", 0) + 1,
        )
        update: dict = {}
        if out.status == RunStatus.BUDGET:
            update = budget_update("build")
        elif out.status != RunStatus.OK:
            update = failure_update("build")
        return Command(goto=route_or_gate(merged(state, update), "verify"), update=update)

    return build
