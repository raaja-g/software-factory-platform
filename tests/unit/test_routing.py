import pytest
from langgraph.types import Send

from swfactory.domain.types import DEFAULT_REVIEWERS, Decision
from swfactory.graph.routing import (
    budget_update,
    failure_update,
    gate_artifacts,
    guard_decision,
    route_after_approve,
    route_after_deliver,
    route_after_human,
    route_after_reviews,
    route_after_verify,
    route_or_gate,
    transition,
)
from swfactory.graph.state import JobState, ReviewOutcome


def s(**kw) -> JobState:
    base: dict = {
        "job_id": "j",
        "round": 1,
        "max_rounds": 2,
        "fix_attempts": 0,
        "max_fix_attempts": 1,
        "reviewers": list(DEFAULT_REVIEWERS),
    }
    base.update(kw)
    return base  # type: ignore[return-value]


def rv(round_, verdicts):
    return {
        str(round_): {
            k: ReviewOutcome(
                kind=k, verdict=v, artifact=f"round-{round_}/review-{k}.md", run_id="r"
            )
            for k, v in zip(DEFAULT_REVIEWERS, verdicts, strict=True)
        }
    }


def test_verify_pass_or_skip_fans_out():
    for status in ("passed", "skipped"):
        assert route_after_verify(s(verify_status=status)) == [
            Send("review", {"job_id": "j", "round": 1, "kind": k})
            for k in ("security", "ux", "ui", "code")
        ]


def test_verify_fail():
    assert route_after_verify(s(verify_status="failed", fix_attempts=0)) == "build"
    assert route_after_verify(s(verify_status="failed", fix_attempts=1)) == "human_gate"
    assert route_after_verify(s(verify_status="error")) == "human_gate"


@pytest.mark.parametrize(
    "round_,verdicts,expected",
    [
        (1, ["PASS"] * 4, "approve"),
        (1, ["CHANGES", "PASS", "PASS", "PASS"], "build"),
        (2, ["CHANGES", "PASS", "PASS", "PASS"], "approve"),
        (1, ["SKIPPED", "PASS", "PASS", "PASS"], "approve"),
        (1, ["ERROR", "CHANGES", "PASS", "PASS"], "human_gate"),
    ],
)
def test_reviews_rounds(round_, verdicts, expected):
    assert route_after_reviews(s(round=round_, reviews=rv(round_, verdicts))) == expected


def test_approve():
    assert [route_after_approve(s(decision=d)) for d in ("APPROVE", "ESCALATE", None)] == [
        "deliver",
        "human_gate",
        "human_gate",
    ]


def test_guard_decision():
    ch = ["CHANGES", "PASS", "PASS", "PASS"]
    assert guard_decision(
        s(round=2, reviews=rv(2, ["SKIPPED", "PASS", "PASS", "PASS"])), Decision.APPROVE
    ) == (Decision.APPROVE, None)
    assert guard_decision(s(round=2, reviews=rv(2, ch)), Decision.APPROVE) == (
        Decision.ESCALATE,
        "outstanding verdicts: security CHANGES",
    )
    assert guard_decision(s(round=2, reviews=rv(2, ch)), Decision.ESCALATE) == (
        Decision.ESCALATE,
        None,
    )


@pytest.mark.parametrize(
    "action,retry_node,expected",
    [
        ("approve", None, "deliver"),
        ("rework", None, "build"),
        ("reject", None, "finalize"),
        ("retry", "spec", "spec"),
        ("retry", None, "human_gate"),
        ("add_budget", "build", "build"),
    ],
)
def test_human(action, retry_node, expected):
    assert route_after_human(s(human={"action": action}, retry_node=retry_node)) == expected


def test_deliver():
    assert [route_after_deliver(s(outcome=o)) for o in ("merged", "delivered")] == [
        "finalize",
        "finalize",
    ]
    assert route_after_deliver(s(gate_reason="merge conflict")) == "human_gate"


def test_transitions():
    assert transition(s(fix_attempts=0), "verify", "build") == {"fix_attempts": 1}
    assert transition(s(round=1), "after_reviews", "build") == {"round": 2, "fix_attempts": 0}
    assert transition(s(), "after_reviews", "human_gate") == {
        "gate_reason": "reviewer failed",
        "retry_node": "verify",
    }
    assert transition(s(), "verify", "human_gate") == {
        "gate_reason": "checks failed",
        "retry_node": "verify",
    }
    assert transition(s(), "approve", "human_gate") == {
        "gate_reason": "approver escalated",
        "retry_node": None,
    }
    assert transition(s(round=2, human={"action": "rework"}), "human_gate", "build") == {
        "round": 3,
        "max_rounds": 3,
        "fix_attempts": 0,
        "gate_reason": None,
        "retry_node": None,
    }
    assert (
        transition(s(human={"action": "reject"}), "human_gate", "finalize")["outcome"] == "closed"
    )


def test_budget_and_failures():
    assert route_or_gate(s(gate_reason="budget"), "verify") == "human_gate"
    assert route_or_gate(s(), "verify") == "verify"
    assert budget_update("build") == {"gate_reason": "budget", "retry_node": "build"}
    assert failure_update("deliver") == {
        "gate_reason": "step failed: deliver",
        "retry_node": "deliver",
    }
    assert failure_update("review")["retry_node"] == "verify"


def test_gate_artifacts():
    err_no_file = ReviewOutcome(kind="ux", verdict="ERROR", artifact="", run_id="r")
    ok_code = ReviewOutcome(
        kind="code", verdict="PASS", artifact="round-1/review-code.md", run_id="r"
    )
    assert gate_artifacts(
        s(
            gate_reason="reviewer failed",
            round=1,
            reviews={"1": {"ux": err_no_file, "code": ok_code}},
        )
    ) == ["round-1/review-code.md"]
    assert gate_artifacts(s(gate_reason="checks failed")) == ["verify.md"]
    r2 = rv(2, ["PASS"] * 4)
    r2_paths = [o["artifact"] for o in r2["2"].values()]
    assert gate_artifacts(s(gate_reason="approver escalated", round=2, reviews=r2)) == [
        "decision.md",
        *sorted(r2_paths),
    ]
    assert gate_artifacts(s(gate_reason="step failed: prepare")) == []
