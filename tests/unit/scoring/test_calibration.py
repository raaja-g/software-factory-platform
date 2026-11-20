from decimal import Decimal

from swfactory.scoring.calibration import (
    Calibration,
    calibration_tag,
    counted_judge_rows,
    is_calibrated,
    kappa,
)
from swfactory.scoring.objective import ScoreRow

RS, JS = "r" * 8, "j" * 8
MR_KEY = ("merge-readiness", "gemini-lite@vertex", RS, JS)
SA_KEY = ("spec-adherence", "gemini-lite@vertex", RS, JS)
MR_GEMINI = ScoreRow(
    "merge-readiness",
    "A",
    Decimal(1),
    True,
    "ok",
    judge_model=MR_KEY[1],
    rubric_sha=RS,
    judge_sha=JS,
)
SA_GEMINI = ScoreRow(
    "spec-adherence",
    "A",
    Decimal(1),
    True,
    "ok",
    judge_model=SA_KEY[1],
    rubric_sha=RS,
    judge_sha=JS,
)
OBJ_GATE = ScoreRow("obj.gate", "passed", Decimal(1), True, "ok")
CAL_OK = Calibration(*MR_KEY, 30, 15, 15, 0.7, True)
CAL_LOW = Calibration(*SA_KEY, 30, 15, 15, 0.2, False)


def test_kappa():
    assert kappa([(True, True), (False, True), (False, False), (True, True)]) == 0.5
    assert kappa([]) is None
    assert kappa([(True, True)] * 3) is None


def test_calibrated_needs_count_and_kappa():
    assert not is_calibrated(29, 1.0, min_class=10)
    assert not is_calibrated(30, 0.59, min_class=10)
    assert not is_calibrated(30, None, min_class=10)
    assert is_calibrated(30, 0.6, min_class=10)


def test_one_sided_labels_never_calibrate():
    assert not is_calibrated(30, 1.0, min_class=1)
    assert not is_calibrated(30, 0.9, min_class=4)
    assert is_calibrated(30, 0.6, min_class=5)


def test_counted_rows_drop_uncalibrated():
    table = {MR_KEY: CAL_OK, SA_KEY: CAL_LOW}
    assert [r.scorer for r in counted_judge_rows([MR_GEMINI, SA_GEMINI], table)] == [
        "merge-readiness"
    ]


def test_objective_rows_never_passed_through():
    assert counted_judge_rows([OBJ_GATE], {}) == []


def test_tags():
    assert calibration_tag(CAL_OK) == "calibrated · κ 0.70"
    assert calibration_tag(None) == "uncalibrated (0 of 30 labels)"
    one_sided = Calibration(*MR_KEY, 30, 29, 1, 0.9, False)
    assert "needs 5 passing and 5 failing" in calibration_tag(one_sided)
