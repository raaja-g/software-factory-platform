from __future__ import annotations

from langgraph.types import Command

from swfactory.domain.types import Stage
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid, merged
from swfactory.graph.routing import route_after_deliver
from swfactory.graph.state import JobState
from swfactory.graph.steps import _runner, pinned_definition

REASON_CONFLICT = "merge conflict"


def make(deps: Deps):
    async def deliver(state: JobState) -> Command:
        job_id = job_uuid(state)
        await enter_stage(deps, job_id, Stage.DELIVERING)
        job, defn, app = await pinned_definition(deps, job_id)
        box = await deps.sandboxes.ensure(job_id, _runner(defn, app))
        update: dict
        if not await deps.git.rebase(job, app, box):
            update = {"gate_reason": REASON_CONFLICT, "retry_node": "deliver"}
        else:
            res = await deps.git.deliver(
                job, app, box, draft=False, expected_head=state.get("approved_head") or ""
            )
            if res.status == "merged":
                update = {"outcome": Stage.MERGED.value}
            elif res.status == "pr_open":
                update = {"outcome": Stage.DELIVERED.value}
                await enter_stage(deps, job_id, Stage.DELIVERED, pr_url=res.pr_url)
            elif res.status == "conflict":
                update = {"gate_reason": REASON_CONFLICT, "retry_node": "deliver"}
            else:
                update = {"gate_reason": f"delivery failed: {res.detail}", "retry_node": "deliver"}
        return Command(goto=route_after_deliver(merged(state, update)), update=update)

    return deliver
