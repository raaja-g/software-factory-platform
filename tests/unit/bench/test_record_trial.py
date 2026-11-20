"""M10.11: how a finished benchmark job becomes a result row (judge columns and status)."""

from __future__ import annotations

from decimal import Decimal

from swfactory.bench.runner import trial_judges, trial_status
from swfactory.scoring.calibration import Calibration
from swfactory.scoring.objective import ScoreRow


def _judge(scorer: str, passed: bool, *, calibrated: bool):
    row = ScoreRow(
        scorer,
        "A" if passed else "F",
        Decimal(1 if passed else 0),
        passed,
        "r",
        judge_model="m@v",
        rubric_sha="r",
        judge_sha="j",
    )
    return row, calibrated


def record(judges=(), gate_reason=None, scope=None):
    rows = [r for r, _ in judges]
    table = {
        (r.scorer, "m@v", "r", "j"): Calibration(r.scorer, "m@v", "r", "j", 30, 15, 15, 0.9, cal)
        for r, cal in judges
    }
    judge_pass, uncal = trial_judges(rows, table)
    return {
        "judge_pass": judge_pass,
        "judge_pass_uncalibrated": uncal,
        "status": trial_status(gate_reason, scope),
    }


def passing(name, *, calibrated):
    return _judge(name, True, calibrated=calibrated)


def failing(name, *, calibrated):
    return _judge(name, False, calibrated=calibrated)


def test_uncalibrated_judge_not_counted():
    r = record(judges=[failing("merge-readiness", calibrated=False)])
    assert (r["judge_pass"], r["judge_pass_uncalibrated"]) == (None, False)


def test_calibrated_judge_counted():
    judges = [
        passing("merge-readiness", calibrated=True),
        failing("spec-adherence", calibrated=False),
    ]
    r = record(judges=judges)
    assert r["judge_pass"] is True and r["judge_pass_uncalibrated"] is False


def test_failing_calibrated_judge_fails_the_trial():
    assert record(judges=[failing("merge-readiness", calibrated=True)])["judge_pass"] is False


def test_no_judges_gives_nulls():
    r = record()
    assert (r["judge_pass"], r["judge_pass_uncalibrated"]) == (None, None)


def test_budget_stopped_trial_is_not_run():
    assert record(gate_reason="budget", scope="bench_run")["status"] == "budget_stopped"


def test_job_cap_stop_is_a_completed_trial():
    assert record(gate_reason="budget", scope="job")["status"] == "done"
    assert record(gate_reason="budget", scope="run")["status"] == "done"
    assert record(gate_reason="approver escalated")["status"] == "done"
