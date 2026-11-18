"""Pure routing functions for the job graph. No I/O."""

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
from swfactory.graph.state import JobState, ReviewTask

_OK_VERDICTS = frozenset({Verdict.PASS.value, Verdict.SKIPPED.value})
REASON_REVIEWER_FAILED = "reviewer failed"
REASON_CHECKS_FAILED = "checks failed"
REASON_APPROVER_ESCALATED = "approver escalated"
REASON_BUDGET = "budget"
REASON_HUMAN_APPROVAL = "human approval required (triage)"
REASON_DIFF_TOO_LARGE = "diff too large to review fully"


def _current_reviews(state: JobState) -> dict[str, Any]:
    return dict(state.get("reviews", {}).get(str(state.get("round", 1)), {}))


def _review_kinds(state: JobState) -> list[str]:
    return list(state.get("reviewers") or DEFAULT_REVIEWERS)


def route_after_verify(state: JobState) -> str | list[Send]:
    status = state.get("verify_status")
    if status in (VerifyStatus.PASSED.value, VerifyStatus.SKIPPED.value):
        rnd, job_id = state.get("round", 1), state.get("job_id", "")
        return [
            Send("review", ReviewTask(job_id=job_id, round=rnd, kind=k))
            for k in _review_kinds(state)
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
            return {"gate_reason": REASON_CHECKS_FAILED, "retry_node": "verify"}
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
    if reason == REASON_CHECKS_FAILED:
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
