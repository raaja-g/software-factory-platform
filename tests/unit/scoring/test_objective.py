from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from swfactory.scoring.objective import MEASURES, JobFacts, ScoreRow, compute_signals

LAST_BUILDER_RUN = uuid4()
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def run(role, rnd, attempt=1, *, status="ok", verdict=None, fb=False, id=None, error=None):
    return {
        "id": id or uuid4(),
        "role": role,
        "round": rnd,
        "attempt": attempt,
        "status": status,
        "verdict": verdict,
        "fallback_detected": fb,
        "cost_usd": Decimal("0.1"),
        "error": error,
    }


def gate(rnd, att, status="passed"):
    return {
        "path": f"verify/r{rnd}-a{att}.json",
        "round": rnd,
        "attempt": att,
        "status": status,
        "reason": None,
        "exit_code": 0 if status == "passed" else 1,
        "protected": [],
        "screenshots": [],
    }


def facts(**kw):
    base = {
        "job": {
            "stage": "merged",
            "round": 2,
            "merge_policy": "local_merge",
            "cost_usd": Decimal("2.10"),
            "started_at": T0,
            "finished_at": T0 + timedelta(minutes=34),
        },
        "runs": [
            run("builder", 1),
            run("reviewer-security", 1, verdict="CHANGES"),
            run("builder", 2, id=LAST_BUILDER_RUN),
            run("reviewer-security", 2, verdict="PASS"),
        ],
        "gates": [gate(1, 1, "failed"), gate(1, 2), gate(2, 1)],
        "human_actions": [],
        "decision_event": {"decision": "APPROVE", "round": 2, "head_sha": "a" * 40},
        "delivery": {"merge_sha": "f" * 40, "head_sha": "a" * 40, "pr_url": None},
    }
    base.update(kw)
    return JobFacts(**base)


FACTS_003 = facts()


def by_scorer(rows: list[ScoreRow]) -> dict[str, ScoreRow]:
    return {r.scorer: r for r in rows}


def test_section_11_example():
    r = by_scorer(compute_signals(FACTS_003))
    assert (r["obj.fix_attempts"].score, r["obj.rounds"].score) == (1, 2)
    assert (r["obj.verdict.security"].label, r["obj.verdict.security"].passed) == (
        "CHANGES>PASS",
        True,
    )
    assert r["obj.verdict.security"].score == Decimal("0.5")
    assert (r["obj.decision"].label, r["obj.human"].label, r["obj.delivery"].label) == (
        "APPROVE",
        "none",
        "merged_unchanged",
    )
    assert r["obj.fallback"].label == "none"
    assert r["obj.duration_s"].score == 2040 and r["obj.cost_usd"].score == Decimal("2.10")
    assert r["obj.gate"].label == "passed"
    assert r["obj.gate"].reason == "tests: exit 0 · protected paths: clean · screenshots: 0"


def test_measures_have_no_pass():
    rows = compute_signals(FACTS_003)
    assert {x.passed for x in rows if x.scorer in MEASURES} == {None}


def test_job_rows_point_at_last_builder_run():
    assert by_scorer(compute_signals(FACTS_003))["obj.decision"].run_id == LAST_BUILDER_RUN


def run_row(r):
    return next(x for x in compute_signals(facts(runs=[r])) if x.scorer == "obj.run")


def test_reviewer_error_run_fails():
    r = run_row(run("reviewer-ux", 1, verdict="ERROR"))
    assert r.label == "error" and r.passed is False


def test_one_run_row_per_agent_run():
    rows = [x for x in compute_signals(FACTS_003) if x.scorer == "obj.run"]
    assert len(rows) == 4 and all(x.run_id for x in rows)


def human(actions):
    r = by_scorer(compute_signals(facts(human_actions=actions)))["obj.human"]
    return r.label, r.passed


def test_human_rework_and_override():
    assert human(["rework", "approve"]) == ("rework+override", False)
    assert human(["approve"]) == ("override", False)
    assert human(["reject"]) == ("reject", False)


def delivery_row(*, stage="merged", policy="local_merge", reviewed=None, merged=None):
    job = {**FACTS_003.job, "stage": stage, "merge_policy": policy}
    f = facts(
        job=job,
        decision_event={"decision": "APPROVE", "head_sha": reviewed},
        delivery={"head_sha": merged},
    )
    r = by_scorer(compute_signals(f))["obj.delivery"]
    return r.scorer, r.label, r.score, r.passed


def delivery(**kw):
    _, label, score, _ = delivery_row(**kw)
    return label, score


def test_pr_merged_unchanged():
    assert delivery(policy="pr_manual", reviewed="a" * 40, merged="a" * 40) == (
        "merged_unchanged",
        Decimal("1.0"),
    )


def test_pr_merged_after_new_push_is_changed():
    assert delivery(policy="pr_manual", reviewed="a" * 40, merged="b" * 40) == (
        "merged_changed",
        Decimal("0.5"),
    )


def test_pr_head_not_recorded():
    assert delivery(policy="pr_manual", reviewed=None, merged="b" * 40)[0] == "merged"


def test_auto_merge_is_unchanged():
    assert delivery(policy="pr_auto_merge", reviewed="a" * 40, merged="b" * 40)[0] == (
        "merged_unchanged"
    )


def test_delivered_waiting_is_pending():
    assert delivery_row(stage="delivered", policy="pr_manual")[1:] == ("delivered", None, None)


def test_other_stage_scores_zero():
    assert delivery_row(stage="closed", policy="pr_manual")[1:] == ("closed", Decimal(0), False)


def test_fallback_counted():
    runs = [run("builder", 1, fb=i < 2) for i in range(9)]
    r = by_scorer(compute_signals(facts(runs=runs)))["obj.fallback"]
    assert r.label == "2 of 9 runs" and r.passed is False


def test_gate_rows_read_m4_verify_json():
    assert [g["path"] for g in FACTS_003.gates] == [
        "verify/r1-a1.json",
        "verify/r1-a2.json",
        "verify/r2-a1.json",
    ]


def test_skipped_gate_half_score():
    r = by_scorer(compute_signals(facts(gates=[gate(1, 1, "skipped")])))["obj.gate"]
    assert (r.score, r.passed) == (Decimal("0.5"), True)
