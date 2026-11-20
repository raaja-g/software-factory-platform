"""Judge calibration: a judge counts only after it agrees with your labels (§11)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from swfactory.scoring.objective import ScoreRow

__all__ = [
    "CALIBRATION_MIN_KAPPA",
    "CALIBRATION_MIN_LABELS",
    "CALIBRATION_MIN_PER_CLASS",
    "Calibration",
    "calibration_tag",
    "counted_judge_rows",
    "is_calibrated",
    "kappa",
]

CALIBRATION_MIN_LABELS = 30
CALIBRATION_MIN_KAPPA = 0.6
CALIBRATION_MIN_PER_CLASS = 5


@dataclass(frozen=True, slots=True)
class Calibration:
    scorer: str
    judge_model: str
    rubric_sha: str
    judge_sha: str
    n: int
    human_pass: int
    human_fail: int
    kappa: float | None
    calibrated: bool


def kappa(pairs: Sequence[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa on pass/fail; None with no pairs or when chance agreement is 1."""
    n = len(pairs)
    if n == 0:
        return None
    p_o = sum(1 for j, h in pairs if j == h) / n
    pj = sum(1 for j, _ in pairs if j) / n
    ph = sum(1 for _, h in pairs if h) / n
    p_e = pj * ph + (1 - pj) * (1 - ph)
    if p_e == 1:
        return None
    return (p_o - p_e) / (1 - p_e)


def is_calibrated(n: int, kappa: float | None, *, min_class: int) -> bool:
    return (
        n >= CALIBRATION_MIN_LABELS
        and kappa is not None
        and kappa >= CALIBRATION_MIN_KAPPA - 1e-9
        and min_class >= CALIBRATION_MIN_PER_CLASS
    )


def counted_judge_rows(
    rows: Sequence[ScoreRow], table: Mapping[tuple[str, str, str, str], Calibration]
) -> list[ScoreRow]:
    """Judge rows whose (scorer, judge_model, rubric_sha, judge_sha) is calibrated."""
    out: list[ScoreRow] = []
    for r in rows:
        if r.judge_model is None or r.rubric_sha is None or r.judge_sha is None:
            continue
        c = table.get((r.scorer, r.judge_model, r.rubric_sha, r.judge_sha))
        if c is not None and c.calibrated:
            out.append(r)
    return out


def calibration_tag(c: Calibration | None) -> str:
    n = 0 if c is None else c.n
    if c is not None and c.calibrated:
        return f"calibrated · κ {c.kappa:.2f}"
    if (
        c is not None
        and c.n >= CALIBRATION_MIN_LABELS
        and min(c.human_pass, c.human_fail) < (CALIBRATION_MIN_PER_CLASS)
    ):
        return (
            f"uncalibrated (needs {CALIBRATION_MIN_PER_CLASS} passing and "
            f"{CALIBRATION_MIN_PER_CLASS} failing labels)"
        )
    return f"uncalibrated ({n} of {CALIBRATION_MIN_LABELS} labels)"
