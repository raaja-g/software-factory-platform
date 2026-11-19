from __future__ import annotations

from langgraph.types import Command, Send
from sqlalchemy import select

from swfactory.db.tables import AgentRun
from swfactory.domain.types import Role, Stage, Verdict
from swfactory.events import record_event
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid, merged
from swfactory.graph.routing import route_after_verify, transition
from swfactory.graph.state import JobState, ReviewOutcome, ReviewTask
from swfactory.graph.steps import (
    _runner,
    job_sandbox,
    pinned_definition,
    refresh_head,
    step_timeout_s,
)
from swfactory.models.triage import reviewers_for, triage_from_json
from swfactory.sandbox.base import running
from swfactory.verify.gate import GateResult, VerifyGate
from swfactory.verify.smoke import SmokeRunner

VERIFY_TIMEOUT_KEY = "verify"
_NO_TRIAGE = {"kind": "feature", "size": "M", "risk": "low", "touches_ui": False}


async def _builder_trailers(deps: Deps, job_id) -> dict[str, str] | None:
    async with deps.sessions() as s:
        model = (
            await s.execute(
                select(AgentRun.requested_model)
                .where(AgentRun.job_id == job_id, AgentRun.role == str(Role.BUILDER))
                .order_by(AgentRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if not model:
        return None
    return {"Factory-Job": str(job_id), "Factory-Role": "builder", "Factory-Model": model}


def make(deps: Deps):
    async def verify(state: JobState) -> Command:
        job_id = job_uuid(state)
        await enter_stage(deps, job_id, Stage.VERIFY)
        job, defn, app = await pinned_definition(deps, job_id)
        rnd = state.get("round", 1)
        runner = defn.runners.get(app.runner)
        box = await job_sandbox(deps, job_id)
        trailers = await _builder_trailers(deps, job_id)
        async with running(box):
            result: GateResult = await VerifyGate(
                deps.git, deps.artifacts, deps.sandboxes, SmokeRunner()
            ).run(
                job,
                app,
                box,
                runner=_runner(defn, app),
                round=rnd,
                attempt=state.get("fix_attempts", 0) + 1,
                setup=runner.setup_commands if runner is not None else (),
                test_timeout_s=step_timeout_s(defn, VERIFY_TIMEOUT_KEY),
                trailers=trailers,
            )
        async with running(box):  # the gate rebased the branch: the shown head follows it
            await refresh_head(deps, job_id, box)
        update: dict = {
            "verify_status": result.status.value,
            "verify_reason": result.reason,
            "screenshots": list(result.screenshots),
        }
        new = merged(state, update)
        target = route_after_verify(new)
        if isinstance(target, list):
            tri = job.triage if isinstance(job.triage, dict) else {}
            plan = reviewers_for(
                app, triage_from_json(tri if "kind" in tri else _NO_TRIAGE), result.paths
            )
            update["reviewers"] = list(plan.run)
            skipped = {
                kind: ReviewOutcome(
                    kind=kind, verdict=Verdict.SKIPPED.value, artifact="", run_id=""
                )
                for kind in plan.skipped
            }
            if skipped:
                update["reviews"] = {str(rnd): skipped}
                async with deps.sessions.begin() as s:
                    for kind in skipped:
                        await record_event(
                            s,
                            job_id,
                            "review.verdict",
                            {"kind": kind, "round": rnd, "verdict": "SKIPPED", "run_id": None},
                        )
            await enter_stage(deps, job_id, Stage.REVIEW)
            sends = [
                Send(
                    "review",
                    ReviewTask(
                        job_id=str(job_id),
                        round=rnd,
                        kind=k,
                        screenshots=list(new.get("screenshots", [])),
                        fan_out=len(plan.run),
                    ),
                )
                for k in plan.run
            ]
            return Command(goto=sends, update=update)
        update.update(transition(new, "verify", target))
        return Command(goto=target, update=update)

    return verify
