"""Pure routing functions for the job graph. No I/O."""

from collections.abc import Sequence
from typing import Any

from langgraph.types import Send

from swfactory.domain.types import (
    DEFAULT_REVIEWERS,
    Decision,
    HumanAction,
    Stage,
    Verdict,
    VerifyStatus,
)
from swfactory.gitops.service import (
    REASON_FOLDER_DIRTY,
    REASON_FOLDER_MOVED,
    REASON_HEAD_MISMATCH,
    REASON_NOTHING_TO_DELIVER,
)
from swfactory.graph.state import JobState, ReviewTask
from swfactory.verify.gate import (
    REASON_HEAD_NOT_ON_BRANCH,
    REASON_NOT_ON_BASE,
    REASON_REBASE_CONFLICT,
    REASON_TEST_COMMAND_CHANGED,
)

__all__ = [
    "REASON_FOLDER_DIRTY",
    "REASON_FOLDER_MOVED",
    "REASON_HEAD_MISMATCH",
    "REASON_HEAD_NOT_ON_BRANCH",
    "REASON_NOTHING_TO_DELIVER",
    "REASON_NOT_ON_BASE",
    "REASON_REBASE_CONFLICT",
    "REASON_TEST_COMMAND_CHANGED",
]

_OK_VERDICTS = frozenset({Verdict.PASS.value, Verdict.SKIPPED.value})
REASON_REVIEWER_FAILED = "reviewer failed"
REASON_CHECKS_FAILED = "checks failed"
REASON_SETUP_FAILED = "setup failed"
REASON_MERGE_CONFLICT = "merge conflict"
REASON_APPROVER_ESCALATED = "approver escalated"
REASON_BUDGET = "budget"
REASON_HUMAN_APPROVAL = "human approval required (triage)"
REASON_DIFF_TOO_LARGE = "diff too large to review fully"


def verify_gate_reason(status: str, reason: str | None) -> str:
    """The gate reason for a verify result that did not pass."""
    if status == VerifyStatus.ERROR.value and reason:
        return reason
    return f"{REASON_CHECKS_FAILED}: {reason}" if reason else REASON_CHECKS_FAILED


def deliverable_heads(approved_head: str, rebases: Sequence[tuple[str, str]]) -> frozenset[str]:
    """The approved head plus every head reached from it through the recorded rebases, in order."""
    heads = {approved_head}
    current = approved_head
    for from_sha, to_sha in rebases:
        if from_sha == current:
            current = to_sha
            heads.add(to_sha)
    return frozenset(heads)


def _current_reviews(state: JobState) -> dict[str, Any]:
    return dict(state.get("reviews", {}).get(str(state.get("round", 1)), {}))


def _review_kinds(state: JobState) -> list[str]:
    return list(state.get("reviewers") or DEFAULT_REVIEWERS)


def route_after_verify(state: JobState) -> str | list[Send]:
    status = state.get("verify_status")
    if status in (VerifyStatus.PASSED.value, VerifyStatus.SKIPPED.value):
        rnd, job_id = state.get("round", 1), state.get("job_id", "")
        kinds = _review_kinds(state)
        return [
            Send("review", ReviewTask(job_id=job_id, round=rnd, kind=k, fan_out=len(kinds)))
            for k in kinds
        ]
    if status == VerifyStatus.FAILED.value and state.get("fix_attempts", 0) < state.get(
        "max_fix_attempts", 1
    ):
        return "build"
    return "human_gate"


def route_after_reviews(state: JobState) -> str:
    current = _current_reviews(state)
    verdicts = [
        current[k]["verdict"] if k in current else Verdict.ERROR.value for k in _review_kinds(state)
    ]
    if Verdict.ERROR.value in verdicts:
        return "human_gate"
    if all(v in _OK_VERDICTS for v in verdicts):
        return "approve"
    if state.get("round", 1) < state.get("max_rounds", 2):
        return "build"
    return "approve"


def route_after_approve(state: JobState) -> str:
    if state.get("decision") != Decision.APPROVE.value:
        return "human_gate"
    if state.get("require_human_approval") or state.get("diff_cut"):
        return "human_gate"
    return "deliver"


def route_after_human(state: JobState) -> str:
    action = (state.get("human") or {}).get("action")
    if action == HumanAction.APPROVE.value:
        return "deliver"
    if action == HumanAction.REWORK.value:
        return "build"
    if action in (HumanAction.RETRY.value, HumanAction.ADD_BUDGET.value):
        return state.get("retry_node") or "human_gate"
    if action in (HumanAction.REJECT.value, HumanAction.CANCEL.value):
        return "finalize"
    return "human_gate"


REQUIREMENTS_GATE_REASON = "approve the backlog"


def route_requirements_gate(state: JobState) -> str:
    action = (state.get("human") or {}).get("action")
    if action == HumanAction.APPROVE.value:
        return "finalize"
    if action in (HumanAction.REJECT.value, HumanAction.CANCEL.value):
        return "finalize"
    if action == HumanAction.REWORK.value:
        return "requirements"
    if action in (HumanAction.RETRY.value, HumanAction.ADD_BUDGET.value):
        return state.get("retry_node") or "requirements"
    return "human_gate"


def route_after_deliver(state: JobState) -> str:
    if state.get("outcome") in (Stage.MERGED.value, Stage.DELIVERED.value):
        return "finalize"
    if state.get("gate_reason"):
        return "human_gate"
    return "finalize"


def route_or_gate(state: JobState, next_node: str) -> str:
    """A step that stopped on a gate reason (budget, no model) goes to the gate."""
    return "human_gate" if state.get("gate_reason") else next_node


def transition(state: JobState, source: str, target: str) -> dict[str, Any]:
    """State updates for the edge source -> target; the route_* functions pick the target."""
    if target == "build":
        if source == "verify":
            return {"fix_attempts": state.get("fix_attempts", 0) + 1}
        if source == "after_reviews":
            return {"round": state.get("round", 1) + 1, "fix_attempts": 0}
        if source == "human_gate":
            new_round = state.get("round", 1) + 1
            return {
                "round": new_round,
                "max_rounds": max(state.get("max_rounds", 2), new_round),
                "fix_attempts": 0,
                "gate_reason": None,
                "retry_node": None,
            }
    if target == "human_gate":
        if source == "after_reviews":
            return {"gate_reason": REASON_REVIEWER_FAILED, "retry_node": "verify"}
        if source == "verify":
            reason = verify_gate_reason(
                state.get("verify_status", VerifyStatus.FAILED.value), state.get("verify_reason")
            )
            return {"gate_reason": reason, "retry_node": "verify"}
        if source == "approve":
            approved = state.get("decision") == Decision.APPROVE.value
            if approved and state.get("diff_cut"):
                reason = REASON_DIFF_TOO_LARGE
            elif approved and state.get("require_human_approval"):
                reason = REASON_HUMAN_APPROVAL
            else:
                reason = REASON_APPROVER_ESCALATED
            return {"gate_reason": reason, "retry_node": None}
    if source == "human_gate":
        update: dict[str, Any] = {"gate_reason": None, "retry_node": None}
        if target == "finalize":
            update["outcome"] = Stage.CLOSED.value
        return update
    return {}


def _retry_target(node: str) -> str:
    return "verify" if node == "review" else node


def failure_update(node: str) -> dict[str, str | None]:
    return {"gate_reason": f"step failed: {node}", "retry_node": _retry_target(node)}


def budget_update(node: str) -> dict[str, str | None]:
    return {"gate_reason": REASON_BUDGET, "retry_node": _retry_target(node)}


def gate_artifacts(state: JobState) -> list[str]:
    reason = state.get("gate_reason")
    paths = {o["artifact"] for o in _current_reviews(state).values() if o.get("artifact")}
    if reason == REASON_REVIEWER_FAILED:
        return sorted(paths)
    if reason and (
        reason.startswith(REASON_CHECKS_FAILED)
        or reason
        in (
            REASON_REBASE_CONFLICT,
            REASON_TEST_COMMAND_CHANGED,
            REASON_HEAD_NOT_ON_BRANCH,
            REASON_NOT_ON_BASE,
            REASON_SETUP_FAILED,
        )
    ):
        return ["verify.md"]
    if reason == REASON_APPROVER_ESCALATED:
        return ["decision.md", *sorted(paths)]
    return []


def guard_decision(state: JobState, decision: Decision) -> tuple[Decision, str | None]:
    if decision != Decision.APPROVE:
        return decision, None
    current = _current_reviews(state)
    bad = [
        f"{k} {current[k]['verdict']}"
        for k in DEFAULT_REVIEWERS
        if k in current and current[k]["verdict"] not in _OK_VERDICTS
    ]
    if not bad:
        return decision, None
    return Decision.ESCALATE, "outstanding verdicts: " + ", ".join(bad)
