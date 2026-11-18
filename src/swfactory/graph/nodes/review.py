from __future__ import annotations

from uuid import UUID

import structlog

from swfactory.domain.types import RunStatus, ToolPolicy, Verdict, reviewer_role
from swfactory.events import record_event
from swfactory.graph import steps
from swfactory.graph.deps import Deps
from swfactory.graph.state import ReviewOutcome, ReviewTask
from swfactory.graph.steps import (
    _runner,
    isolate_instructions,
    pinned_definition,
    review_attachments,
    reviewer_box_name,
    run_agent_step,
)
from swfactory.sandbox.paths import REPO

log = structlog.get_logger(__name__)


def make(deps: Deps):
    async def review(task: ReviewTask) -> dict:
        """One reviewer. Never raises: any failure is recorded as an ERROR verdict (R01)."""
        kind, rnd = task["kind"], task["round"]
        out_name = f"round-{rnd}/review-{kind}.md"
        outcome = ReviewOutcome(kind=kind, verdict=Verdict.ERROR.value, artifact="", run_id="")
        try:
            job_id = UUID(task["job_id"])
            role = reviewer_role(kind)
            inputs = ["feature.md", "app.md", "spec.md", "build.md", "verify.md"]
            attach = review_attachments(kind, task.get("screenshots", []))
            engine = steps.role_engine(deps, job_id, role)
            fan_out = task.get("fan_out", 1)
            if engine == "direct":
                # no box: the direct engine reads the attachments from the job box
                res = await run_agent_step(
                    deps,
                    job_id=job_id,
                    role=role,
                    round=rnd,
                    inputs=inputs,
                    output_name=out_name,
                    workdir=None,
                    tool_policy=ToolPolicy.NONE,
                    attachments=attach,
                    engine=engine,
                    fan_out=fan_out,
                )
            else:
                _, defn, app = await pinned_definition(deps, job_id)
                async with deps.sandboxes.isolated(
                    job_id, _runner(defn, app), name=reviewer_box_name(kind, rnd)
                ) as rbox:
                    await isolate_instructions(deps, job_id, rbox)
                    res = await run_agent_step(
                        deps,
                        job_id=job_id,
                        role=role,
                        round=rnd,
                        inputs=inputs,
                        output_name=out_name,
                        workdir=REPO,
                        tool_policy=ToolPolicy.READ_RUN,
                        sandbox=rbox,
                        attachments=attach,
                        engine=engine,
                        fan_out=fan_out,
                    )
            outcome["run_id"] = str(res.run_id)
            if res.status == RunStatus.OK and res.verdict is not None:
                outcome["verdict"] = res.verdict.value
                outcome["artifact"] = out_name
                if res.diff_cut:
                    outcome["diff_cut"] = True
        except Exception as exc:
            log.warning("review.error", kind=kind, error=str(exc))
        try:
            async with deps.sessions.begin() as s:
                await record_event(
                    s,
                    UUID(task["job_id"]),
                    "review.verdict",
                    {
                        "kind": kind,
                        "round": rnd,
                        "verdict": outcome["verdict"],
                        "run_id": outcome["run_id"] or None,
                    },
                )
        except Exception as exc:
            log.warning("review.event_failed", kind=kind, error=str(exc))
        return {"reviews": {str(rnd): {kind: outcome}}}

    return review
