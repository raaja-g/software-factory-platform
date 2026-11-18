from __future__ import annotations

from uuid import UUID

import structlog

from swfactory.domain.types import RunStatus, ToolPolicy, Verdict, reviewer_role
from swfactory.events import record_event
from swfactory.graph.deps import Deps
from swfactory.graph.state import ReviewOutcome, ReviewTask
from swfactory.graph.steps import run_agent_step
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
            res = await run_agent_step(
                deps,
                job_id=job_id,
                role=reviewer_role(kind),
                round=rnd,
                inputs=["feature.md", "spec.md", "build.md", "verify.md"],
                output_name=out_name,
                workdir=REPO,
                tool_policy=ToolPolicy.READ_ONLY,
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
