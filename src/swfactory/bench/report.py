"""Benchmark report: per-configuration summary, Pareto set and the recommendation (section 12)."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

MIN_TRIALS = 3
QUALITY_TOLERANCE = 0.05
MIN_QUALITY = 0.5
_NOT_RUN = ("skipped_budget", "budget_stopped", "skipped_invalid")
_WILSON_Z = 1.96


@dataclass(frozen=True, slots=True)
class ConfigSummary:
    name: str
    trials: int
    completed: int
    not_run: int
    hidden_pass_rate: float | None
    judge_pass_rate: float | None
    judge_pass_rate_uncalibrated: float | None
    quality: float | None
    mean_rounds: float | None
    total_cost_usd: Decimal
    mean_cost_usd: Decimal | None
    median_duration_s: float | None
    hidden_pass_interval: tuple[float, float] | None = None
    task_pass: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BenchReport:
    run_id: UUID
    suite: str
    definition_sha: str
    status: str
    budget_usd: Decimal
    cost_usd: Decimal
    configs: tuple[ConfigSummary, ...]
    chart: tuple[Mapping[str, Any], ...]
    pareto: tuple[str, ...]
    recommended: str | None
    recommendation_reason: str
    lowest_cost: str | None
    best_quality: str | None
    common_tasks: tuple[str, ...] = ()
    invalid_tasks: tuple[str, ...] = ()


def wilson_interval(passed: int, n: int) -> tuple[float, float] | None:
    """95% Wilson score interval of a pass rate."""
    if n <= 0:
        return None
    p = passed / n
    z2 = _WILSON_Z**2
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = _WILSON_Z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _money(v: Any) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal(0)


def _is_completed(r: Mapping[str, Any]) -> bool:
    return r.get("status") == "done" and r.get("hidden_passed") is not None


def _rate(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    known = [bool(r[key]) for r in rows if r.get(key) is not None]
    return sum(known) / len(known) if known else None


def _summary(name: str, rows: Sequence[Mapping[str, Any]], common: frozenset[str]) -> ConfigSummary:
    done = [r for r in rows if _is_completed(r)]
    counted = [r for r in done if r["task"] in common]
    not_run = sum(1 for r in rows if r.get("status") in _NOT_RUN or _not_checked(r))
    passed = sum(1 for r in counted if r["hidden_passed"])
    rate = passed / len(counted) if counted else None
    by_task: dict[str, list[bool]] = {}
    for r in done:
        by_task.setdefault(r["task"], []).append(bool(r["hidden_passed"]))
    rounds = [r["rounds"] for r in counted if r.get("rounds") is not None]
    durations = [float(r["duration_s"]) for r in counted if r.get("duration_s") is not None]
    costs = [_money(r.get("cost_usd")) for r in counted]
    return ConfigSummary(
        name=name,
        trials=len(rows),
        completed=len(counted),
        not_run=not_run,
        hidden_pass_rate=rate,
        judge_pass_rate=_rate(counted, "judge_pass"),
        judge_pass_rate_uncalibrated=_rate(counted, "judge_pass_uncalibrated"),
        quality=rate,
        mean_rounds=sum(rounds) / len(rounds) if rounds else None,
        total_cost_usd=sum((_money(r.get("cost_usd")) for r in rows), Decimal(0)),
        mean_cost_usd=sum(costs, Decimal(0)) / len(costs) if costs else None,
        median_duration_s=statistics.median(durations) if durations else None,
        hidden_pass_interval=wilson_interval(passed, len(counted)),
        task_pass={t: sum(v) / len(v) for t, v in sorted(by_task.items())},
    )


def _not_checked(r: Mapping[str, Any]) -> bool:
    return r.get("status") == "done" and r.get("hidden_passed") is None


def _pareto(configs: Sequence[ConfigSummary]) -> tuple[str, ...]:
    pts = [c for c in configs if c.quality is not None and c.mean_cost_usd is not None]
    front: list[ConfigSummary] = []
    for c in pts:
        assert c.quality is not None and c.mean_cost_usd is not None
        beaten = any(
            o is not c
            and o.quality is not None
            and o.mean_cost_usd is not None
            and o.quality >= c.quality
            and o.mean_cost_usd <= c.mean_cost_usd
            and (o.quality > c.quality or o.mean_cost_usd < c.mean_cost_usd)
            for o in pts
        )
        if not beaten:
            front.append(c)
    front.sort(key=lambda c: (c.mean_cost_usd, c.name))
    return tuple(c.name for c in front)


def _recommend(configs: Sequence[ConfigSummary]) -> tuple[str | None, str]:
    eligible = [c for c in configs if c.completed >= MIN_TRIALS and c.quality is not None]
    if not eligible:
        return None, f"too few completed trials (need {MIN_TRIALS})"
    best = max(c.quality for c in eligible if c.quality is not None)
    if best < MIN_QUALITY:
        return None, "no configuration passed at least half of its hidden checks"
    near = [c for c in eligible if c.quality is not None and c.quality >= best - QUALITY_TOLERANCE]

    def key(c: ConfigSummary) -> tuple[Decimal, float, str]:
        assert c.mean_cost_usd is not None
        dur = c.median_duration_s if c.median_duration_s is not None else math.inf
        return c.mean_cost_usd, dur, c.name

    pick = min(near, key=key)
    assert pick.quality is not None and pick.mean_cost_usd is not None
    reason = f"{pick.name}: quality {pick.quality:.2f} at ${pick.mean_cost_usd:.2f} per task"
    others = [c for c in eligible if c is not pick and c.mean_cost_usd is not None]
    if others:
        ref = max(others, key=lambda c: (c.quality or 0.0, c.mean_cost_usd or Decimal(0)))
        assert ref.quality is not None and ref.mean_cost_usd is not None
        reason += f" ({ref.name}: {ref.quality:.2f} at ${ref.mean_cost_usd:.2f})"
    return pick.name, reason


def build_report(run: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> BenchReport:
    by_config: dict[str, list[Mapping[str, Any]]] = {}
    for r in results:
        by_config.setdefault(r["configuration"], []).append(r)
    done_tasks = [{r["task"] for r in rows if _is_completed(r)} for rows in by_config.values()]
    common = frozenset(set.intersection(*done_tasks)) if done_tasks else frozenset()
    configs = tuple(_summary(n, rows, common) for n, rows in sorted(by_config.items()))
    pareto = _pareto(configs)
    recommended, reason = _recommend(configs)
    priced = [c for c in configs if c.mean_cost_usd is not None]
    ranked = [c for c in configs if c.quality is not None and c.mean_cost_usd is not None]
    lowest = min(priced, key=lambda c: (c.mean_cost_usd or Decimal(0), c.name), default=None)
    top = min(
        ranked,
        key=lambda c: (-(c.quality or 0.0), c.mean_cost_usd or Decimal(0), c.name),
        default=None,
    )
    chart = tuple(
        {
            "config": c.name,
            "cost_usd": f"{c.mean_cost_usd:.6f}",
            "quality": c.quality,
            "pareto": c.name in pareto,
        }
        for c in ranked
        if c.mean_cost_usd is not None
    )
    invalid = sorted({r["task"] for r in results if r.get("status") == "skipped_invalid"})
    return BenchReport(
        run_id=run["id"] if isinstance(run["id"], UUID) else UUID(str(run["id"])),
        suite=run["suite"],
        definition_sha=run.get("definition_sha") or "",
        status=run["status"],
        budget_usd=_money(run.get("budget_usd")),
        cost_usd=_money(run.get("cost_usd")),
        configs=configs,
        chart=chart,
        pareto=pareto,
        recommended=recommended,
        recommendation_reason=reason,
        lowest_cost=lowest.name if lowest else None,
        best_quality=top.name if top else None,
        common_tasks=tuple(sorted(common)),
        invalid_tasks=tuple(invalid),
    )
