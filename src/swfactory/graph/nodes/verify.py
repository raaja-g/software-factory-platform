from __future__ import annotations

from langgraph.types import Command

from swfactory.domain.types import Stage, VerifyStatus
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid, merged
from swfactory.graph.routing import route_after_verify, transition
from swfactory.graph.state import JobState
from swfactory.graph.steps import _runner, pinned_definition, step_timeout_s

VERIFY_TIMEOUT_KEY = "verify"
_TAIL_CHARS = 4000


def make(deps: Deps):
    async def verify(state: JobState) -> Command:
        job_id = job_uuid(state)
        await enter_stage(deps, job_id, Stage.VERIFY)
        _, defn, app = await pinned_definition(deps, job_id)
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
            await enter_stage(deps, job_id, Stage.REVIEW)
            return Command(goto=target, update=update)
        update.update(transition(new, "verify", target))
        return Command(goto=target, update=update)

    return verify
