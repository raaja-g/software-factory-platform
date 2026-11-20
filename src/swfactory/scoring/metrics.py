"""Metrics over scored jobs: pass rates by dimension, rounds, escalation, cost, scorer health."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from swfactory.scoring.calibration import Calibration
from swfactory.scoring.objective import OBJ_PREFIX

__all__ = ["MetricJob", "MetricScore", "MetricsRows", "summarize_metrics"]

NONE_KEY = "(none)"


@dataclass(frozen=True, slots=True)
class MetricScore:
    """One score row with the run and job dimensions it joins to."""

    scorer: str
    passed: bool | None
    role: str | None = None
    model: str | None = None
    engine: str | None = None
    profile: str | None = None
    judge_model: str | None = None
    rubric_sha: str | None = None
    judge_sha: str | None = None


@dataclass(frozen=True, slots=True)
class MetricJob:
    stage: str
    cost_usd: Decimal
    round: int = 0
    waited: bool = False


@dataclass(frozen=True, slots=True)
class MetricsRows:
    scores: Sequence[MetricScore] = ()
    jobs: Sequence[MetricJob] = ()
    runs: int = 0
    fallbacks: int = 0
    calibration: Mapping[tuple[str, str, str, str], Calibration] = field(default_factory=dict)


def _calibrated(r: MetricScore, table: Mapping[tuple[str, str, str, str], Calibration]) -> bool:
    if r.judge_model is None or r.rubric_sha is None or r.judge_sha is None:
        return False
    c = table.get((r.scorer, r.judge_model, r.rubric_sha, r.judge_sha))
    return c is not None and c.calibrated


def _rates(rows: Sequence[MetricScore], dim: str) -> list[dict[str, Any]]:
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        key = getattr(r, dim)
        if key is None:
            if dim == "profile":
                key = NONE_KEY
            else:
                continue
        t = tally[key]
        t[0] += 1
        t[1] += 1 if r.passed else 0
    return [{"key": k, "n": n, "pass_rate": p / n} for k, (n, p) in sorted(tally.items()) if n > 0]


def _scorers(rows: MetricsRows) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[MetricScore]] = defaultdict(list)
    for r in rows.scores:
        if r.judge_model is not None and r.passed is not None:
            groups[(r.scorer, r.judge_model)].append(r)
    out: list[dict[str, Any]] = []
    for (scorer, model), rs in sorted(groups.items()):
        versions = {(r.rubric_sha, r.judge_sha) for r in rs}
        cals = [
            c
            for (sc, jm, rub, js), c in rows.calibration.items()
            if sc == scorer and jm == model and (rub, js) in versions
        ]
        best = max(cals, key=lambda c: (c.calibrated, c.n), default=None)
        out.append(
            {
                "scorer": scorer,
                "judge_model": model,
                "n": len(rs),
                "pass_rate": sum(1 for r in rs if r.passed) / len(rs),
                "calibrated": any(c.calibrated for c in cals),
                "labels": 0 if best is None else best.n,
                "kappa": None if best is None else best.kappa,
            }
        )
    return out


def summarize_metrics(rows: MetricsRows, *, window_days: int) -> dict[str, Any]:
    counted = [
        r
        for r in rows.scores
        if r.passed is not None
        and (
            (r.judge_model is None and r.scorer.startswith(OBJ_PREFIX))
            or _calibrated(r, rows.calibration)
        )
    ]
    jobs = rows.jobs
    n = len(jobs)
    merged = sum(1 for j in jobs if j.stage == "merged")
    spend = sum((j.cost_usd for j in jobs), Decimal(0))
    hist = Counter(j.round for j in jobs)
    return {
        "available": True,
        "window_days": window_days,
        "jobs": n,
        "pass_rate": {d: _rates(counted, d) for d in ("role", "model", "engine", "profile")},
        "rounds": {
            "mean": sum(j.round for j in jobs) / n if n else None,
            "histogram": [{"rounds": k, "jobs": v} for k, v in sorted(hist.items())],
        },
        "escalation_rate": sum(1 for j in jobs if j.waited) / n if n else None,
        "cost_per_merged_usd": f"{spend / merged:.6f}" if merged else None,
        "fallback_rate": rows.fallbacks / rows.runs if rows.runs else None,
        "scorers": _scorers(rows),
    }
