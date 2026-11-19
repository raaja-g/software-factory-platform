from __future__ import annotations

from typing import Any
from uuid import UUID

from langgraph.types import Command
from sqlalchemy import select

from swfactory.db.tables import Job, JobEvent
from swfactory.domain.types import Stage, VerifyStatus
from swfactory.events import record_event
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import enter_stage, job_uuid, merged
from swfactory.graph.nodes.verify import VERIFY_TIMEOUT_KEY
from swfactory.graph.routing import (
    REASON_FOLDER_DIRTY,
    REASON_FOLDER_MOVED,
    REASON_HEAD_MISMATCH,
    REASON_MERGE_CONFLICT,
    deliverable_heads,
    route_after_deliver,
    verify_gate_reason,
)
from swfactory.graph.state import JobState
from swfactory.graph.steps import (
    _head,
    _runner,
    job_sandbox,
    pinned_definition,
    step_timeout_s,
    store_head,
)
from swfactory.sandbox.base import running
from swfactory.verify.gate import GateResult, VerifyGate
from swfactory.verify.smoke import SmokeRunner

REASON_CONFLICT = REASON_MERGE_CONFLICT
_RETRY = "deliver"


async def record_pr(deps: Deps, job_id: UUID, pr_url: str) -> bool:
    """Store jobs.pr_url; emit delivery.pr_opened and return True only when it was null."""
    async with deps.sessions.begin() as s:
        job = await s.get(Job, job_id, with_for_update=True, populate_existing=True)
        if job is None or job.pr_url:
            return False
        job.pr_url = pr_url
        await s.flush()
        await record_event(s, job_id, "delivery.pr_opened", {"pr_url": pr_url})
        return True


def _gate(reason: str) -> dict[str, Any]:
    return {"gate_reason": reason, "retry_node": _RETRY}


def make(deps: Deps):
    async def _rebases(job_id) -> list[tuple[str, str]]:
        async with deps.sessions() as s:
            rows = await s.scalars(
                select(JobEvent.data)
                .where(JobEvent.job_id == job_id, JobEvent.kind == "delivery.rebased")
                .order_by(JobEvent.id)
            )
            return [(str(d.get("from_sha")), str(d.get("to_sha"))) for d in rows]

    async def _merged_recorded(job_id, merge_sha: str | None) -> bool:
        async with deps.sessions() as s:
            rows = await s.scalars(
                select(JobEvent.data).where(
                    JobEvent.job_id == job_id, JobEvent.kind == "delivery.merged"
                )
            )
            return any(d.get("merge_sha") == merge_sha for d in rows)

    async def deliver(state: JobState) -> Command:
        job_id = job_uuid(state)
        await enter_stage(deps, job_id, Stage.DELIVERING)
        job, defn, app = await pinned_definition(deps, job_id)
        box = await job_sandbox(deps, job_id)
        update: dict[str, Any]
        async with running(box):
            update = await _deliver_in_box(state, job, defn, app, box)
        return Command(goto=route_after_deliver(merged(state, update)), update=update)

    async def _deliver_in_box(state: JobState, job, defn, app, box) -> dict[str, Any]:  # noqa: ANN001
        job_id = job.id
        approved = state.get("approved_head")
        head = await _head(box)
        if not approved or head not in deliverable_heads(approved, await _rebases(job_id)):
            return _gate(REASON_HEAD_MISMATCH)
        if not await deps.git.rebase(job, app, box):
            return _gate(REASON_MERGE_CONFLICT)
        moved = await _head(box)
        if moved != head:
            async with deps.sessions.begin() as s:  # head and event commit together
                await store_head(s, job_id, moved)
                await record_event(
                    s, job_id, "delivery.rebased", {"from_sha": head, "to_sha": moved}
                )
        update: dict[str, Any] = {}
        if moved != approved:  # this rebase, or an earlier interrupted one, moved the head
            runner = defn.runners.get(app.runner)
            result: GateResult = await VerifyGate(
                deps.git, deps.artifacts, deps.sandboxes, SmokeRunner()
            ).run(
                job,
                app,
                box,
                runner=_runner(defn, app),
                round=state.get("round", 1),
                attempt=state.get("fix_attempts", 0) + 2,
                setup=runner.setup_commands if runner is not None else (),
                test_timeout_s=step_timeout_s(defn, VERIFY_TIMEOUT_KEY),
            )
            if result.status not in (VerifyStatus.PASSED, VerifyStatus.SKIPPED):
                return {
                    **_gate(verify_gate_reason(result.status.value, result.reason)),
                    "verify_status": result.status.value,
                    "verify_reason": result.reason,
                }
            approved = moved
            update["approved_head"] = approved
        async with deps.sessions() as s:  # the row loaded at node start is stale after a rebase
            job = await s.get_one(Job, job_id, populate_existing=True)
        res = await deps.git.deliver(job, app, box, draft=False, expected_head=approved)
        if res.status == "merged":
            update["outcome"] = Stage.MERGED.value
            if not await _merged_recorded(job_id, res.merge_sha):
                async with deps.sessions.begin() as s:
                    await record_event(
                        s,
                        job_id,
                        "delivery.merged",
                        {"merge_sha": res.merge_sha, "head_sha": approved, "pr_url": res.pr_url},
                    )
            if res.pr_url:
                await record_pr(deps, job_id, res.pr_url)
        elif res.status == "pr_open":
            update["outcome"] = Stage.DELIVERED.value
            if res.pr_url:
                await record_pr(deps, job_id, res.pr_url)
        elif res.status == "conflict":
            update.update(_gate(REASON_MERGE_CONFLICT))
        elif res.detail in (REASON_FOLDER_DIRTY, REASON_FOLDER_MOVED):
            update.update(_gate(res.detail))
        else:
            update.update(_gate(f"delivery failed: {res.detail}"))
        return update

    return deliver
