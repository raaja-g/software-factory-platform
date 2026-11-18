from __future__ import annotations

from langgraph.types import Command, Send

from swfactory.domain.types import Stage, Verdict, VerifyStatus
from swfactory.events import record_event
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid, merged
from swfactory.graph.routing import route_after_verify, transition
from swfactory.graph.state import JobState, ReviewOutcome, ReviewTask
from swfactory.graph.steps import _runner, pinned_definition, step_timeout_s
from swfactory.models.triage import reviewers_for, triage_from_json

VERIFY_TIMEOUT_KEY = "verify"
_TAIL_CHARS = 4000
_NO_TRIAGE = {"kind": "feature", "size": "M", "risk": "low", "touches_ui": False}


def make(deps: Deps):
    async def verify(state: JobState) -> Command:
        job_id = job_uuid(state)
        await enter_stage(deps, job_id, Stage.VERIFY)
        job, defn, app = await pinned_definition(deps, job_id)
        cmd = app.test_command
        reason: str | None = None
        if not cmd:
            status = VerifyStatus.SKIPPED
        else:
            box = await deps.sandboxes.ensure(job_id, _runner(defn, app))
            res = await box.exec(
                ["sh", "-c", cmd], timeout_s=step_timeout_s(defn, VERIFY_TIMEOUT_KEY)
            )
            status = (
                VerifyStatus.PASSED
                if res.exit_code == 0 and not res.timed_out
                else VerifyStatus.FAILED
            )
            if status == VerifyStatus.FAILED:
                reason = "timed out" if res.timed_out else f"exit code {res.exit_code}"
            tail = (res.stdout + res.stderr)[-_TAIL_CHARS:]
            body = (
                f"# Verify: {status.value}\ncommand: {cmd}\nexit code: {res.exit_code}\n\n{tail}\n"
            )
            await deps.artifacts.put(job_id, "verify.md", body)
        update: dict = {"verify_status": status.value, "verify_reason": reason}
        new = merged(state, update)
        target = route_after_verify(new)
        if isinstance(target, list):
            box = await deps.sandboxes.ensure(job_id, _runner(defn, app))
            stat = await deps.git.diff_stat(box, job.base_sha or app.default_branch)
            tri = job.triage if isinstance(job.triage, dict) else {}
            plan = reviewers_for(
                app, triage_from_json(tri if "kind" in tri else _NO_TRIAGE), stat.paths
            )
            update["reviewers"] = list(plan.run)
            rnd = state.get("round", 1)
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
                Send("review", ReviewTask(job_id=str(job_id), round=rnd, kind=k)) for k in plan.run
            ]
            return Command(goto=sends, update=update)
        update.update(transition(new, "verify", target))
        return Command(goto=target, update=update)

    return verify
