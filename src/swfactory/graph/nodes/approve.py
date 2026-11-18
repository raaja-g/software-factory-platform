from __future__ import annotations

from langgraph.types import Command

from swfactory.domain.types import Decision, Role, RunStatus, Stage, ToolPolicy
from swfactory.events import record_event
from swfactory.graph import steps
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
from swfactory.graph.steps import (
    _head,
    _runner,
    isolate_instructions,
    job_sandbox,
    pinned_definition,
    run_agent_step,
)
from swfactory.sandbox.base import running
from swfactory.sandbox.paths import REPO


def make(deps: Deps):
    async def approve(state: JobState) -> Command:
        job_id = job_uuid(state)
        rnd = state.get("round", 1)
        await enter_stage(deps, job_id, Stage.APPROVE)
        inputs = ["feature.md", "app.md", "spec.md", "build.md", "verify.md"]
        inputs += [
            f"round-{r}/review-{k}.md"
            for r in range(1, rnd + 1)
            for k in state.get("reviews", {}).get(str(r), {})
        ]
        engine = steps.role_engine(deps, job_id, Role.APPROVER)
        if engine == "direct":
            out = await run_agent_step(
                deps,
                job_id=job_id,
                role=Role.APPROVER,
                round=rnd,
                inputs=inputs,
                output_name="decision.md",
                workdir=REPO,
                tool_policy=ToolPolicy.READ_ONLY,
                engine=engine,
            )
        else:
            _, defn, app = await pinned_definition(deps, job_id)
            async with deps.sandboxes.isolated(
                job_id, _runner(defn, app), name=f"approve-r{rnd}"
            ) as abox:
                await isolate_instructions(deps, job_id, abox)
                out = await run_agent_step(
                    deps,
                    job_id=job_id,
                    role=Role.APPROVER,
                    round=rnd,
                    inputs=inputs,
                    output_name="decision.md",
                    workdir=REPO,
                    tool_policy=ToolPolicy.READ_ONLY,
                    sandbox=abox,
                    engine=engine,
                )
        if out.status != RunStatus.OK or out.decision is None:
            update = (
                budget_update("approve")
                if out.status == RunStatus.BUDGET
                else failure_update("approve")
            )
            return Command(goto="human_gate", update=update)
        decision, reason = guard_decision(state, out.decision)
        box = await job_sandbox(deps, job_id)
        async with running(box):
            head = await _head(box)
        data: dict = {"decision": decision.value, "round": rnd, "head_sha": head}
        if reason is not None:
            data.update(overridden=True, reason=reason)
        cut = out.diff_cut or any(
            o.get("diff_cut", False) for o in state.get("reviews", {}).get(str(rnd), {}).values()
        )
        update: dict = {"decision": decision.value, "diff_cut": cut}
        if cut:
            update["require_human_approval"] = True
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
