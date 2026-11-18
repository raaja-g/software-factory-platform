from __future__ import annotations

from langgraph.types import Command

from swfactory.domain.types import Decision, Role, RunStatus, Stage, ToolPolicy
from swfactory.events import record_event
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid, merged
from swfactory.graph.routing import (
    budget_update,
    failure_update,
    guard_decision,
    route_after_approve,
    transition,
)
from swfactory.graph.state import JobState
from swfactory.graph.steps import _head, _runner, pinned_definition, run_agent_step
from swfactory.sandbox.paths import REPO


def make(deps: Deps):
    async def approve(state: JobState) -> Command:
        job_id = job_uuid(state)
        rnd = state.get("round", 1)
        await enter_stage(deps, job_id, Stage.APPROVE)
        _, defn, app = await pinned_definition(deps, job_id)
        inputs = ["feature.md", "spec.md", "build.md", "verify.md"]
        inputs += [
            f"round-{r}/review-{k}.md"
            for r in range(1, rnd + 1)
            for k in state.get("reviews", {}).get(str(r), {})
        ]
        out = await run_agent_step(
            deps,
            job_id=job_id,
            role=Role.APPROVER,
            round=rnd,
            inputs=inputs,
            output_name="decision.md",
            workdir=REPO,
            tool_policy=ToolPolicy.READ_ONLY,
        )
        if out.status != RunStatus.OK or out.decision is None:
            update = (
                budget_update("approve")
                if out.status == RunStatus.BUDGET
                else failure_update("approve")
            )
            return Command(goto="human_gate", update=update)
        decision, reason = guard_decision(state, out.decision)
        box = await deps.sandboxes.ensure(job_id, _runner(defn, app))
        head = await _head(box)
        data: dict = {"decision": decision.value, "round": rnd, "head_sha": head}
        if reason is not None:
            data.update(overridden=True, reason=reason)
        update: dict = {"decision": decision.value}
        if decision == Decision.APPROVE:
            update["approved_head"] = head
        async with deps.sessions.begin() as s:
            await record_event(s, job_id, "decision.made", data)
        await enter_stage(deps, job_id, Stage.APPROVE, decision=decision.value)
        new = merged(state, update)
        target = route_after_approve(new)
        update.update(transition(new, "approve", target))
        return Command(goto=target, update=update)

    return approve
