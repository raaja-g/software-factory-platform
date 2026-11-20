from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from swfactory.bench.report import build_report

RUN_ID = UUID("00000000-0000-0000-0000-0000000000a1")
RUN = {
    "id": RUN_ID,
    "suite": "v1-parity",
    "definition_sha": "a" * 40,
    "status": "done",
    "budget_usd": Decimal("40"),
    "cost_usd": Decimal("20"),
}
TASKS = ("001-bootstrap-python", "002-todo-cli", "003-todo-web", "004-todo-api")


def row(config, task, cost, *, passed: bool | int | None = True, status="done", **kw):
    base = {
        "configuration": config,
        "task": task,
        "status": status,
        "hidden_passed": passed if status == "done" else None,
        "judge_pass": None,
        "judge_pass_uncalibrated": None,
        "rounds": 2,
        "cost_usd": Decimal(cost),
        "duration_s": Decimal("60"),
        "hidden_detail": None,
    }
    base.update(kw)
    return base


def trials(config, cost, passes, tasks=TASKS):
    return [row(config, t, cost, passed=p) for t, p in zip(tasks, passes, strict=True)]


RESULTS = [
    *trials("gemini", "1.20", [True] * 4),
    *trials("claude", "2.40", [True] * 4),
    *trials("economy", "0.50", [True, True, True, False]),
]


def summary(r, name="gemini"):
    return next(c for c in r.configs if c.name == name)


def test_recommends_cheapest_within_tolerance():
    r = build_report(RUN, RESULTS)
    assert (r.recommended, r.lowest_cost, r.pareto) == ("gemini", "economy", ("economy", "gemini"))
    assert r.best_quality == "gemini"
    assert (
        r.recommendation_reason == "gemini: quality 1.00 at $1.20 per task (claude: 1.00 at $2.40)"
    )


def test_quality_is_hidden_checks_only():
    rows = [
        row("a", t, "1", passed=p, judge_pass=False)
        for t, p in zip(TASKS, [1, 1, 1, 0], strict=True)
    ]
    c = summary(build_report(RUN, rows), "a")
    assert (c.quality, c.judge_pass_rate) == (0.75, 0.0)


def test_uncalibrated_judge_leaves_recommendation_unchanged():
    rows = [
        dict(r, judge_pass_uncalibrated=False) if r["configuration"] == "gemini" else r
        for r in RESULTS
    ]
    r = build_report(RUN, rows)
    g = summary(r)
    assert (r.recommended, g.quality) == (build_report(RUN, RESULTS).recommended, 1.0)
    assert (g.judge_pass_rate, g.judge_pass_rate_uncalibrated) == (None, 0.0)


def test_skipped_trials_not_run():
    rows = [
        *trials("a", "1", [True, True], TASKS[:2]),
        *[row("a", t, "0", status="skipped_budget") for t in TASKS[2:]],
    ]
    c = build_report(RUN, rows).configs[0]
    assert (c.not_run, c.completed) == (2, 2)


def test_too_few_trials():
    rows = [r for r in RESULTS if r["task"] in TASKS[:2]]
    assert build_report(RUN, rows).recommendation_reason == "too few completed trials (need 3)"


def test_rates_over_tasks_every_config_completed():
    rows = [r for r in RESULTS if not (r["configuration"] == "economy" and r["task"] == TASKS[3])]
    rows.append(row("economy", TASKS[3], "0", status="budget_stopped"))
    rows = [
        dict(r, hidden_passed=True)
        if r["configuration"] == "economy" and r["status"] == "done"
        else r
        for r in rows
    ]
    r = build_report(RUN, rows)
    e = summary(r, "economy")
    assert r.common_tasks == TASKS[:3]
    assert (e.hidden_pass_rate, e.not_run, e.completed) == (1.0, 1, 3)


def test_interval_shown():
    c = summary(build_report(RUN, RESULTS))
    assert c.hidden_pass_interval is not None
    assert tuple(round(x, 2) for x in c.hidden_pass_interval) == (0.51, 1.0)
    e = summary(build_report(RUN, RESULTS), "economy")
    assert e.hidden_pass_interval is not None
    assert tuple(round(x, 2) for x in e.hidden_pass_interval) == (0.30, 0.95)


def test_no_recommendation_below_quality_floor():
    rows = [*trials("a", "1", [True, False, False, False]), *trials("b", "2", [False] * 4)]
    r = build_report(RUN, rows)
    assert (r.recommended, r.recommendation_reason) == (
        None,
        "no configuration passed at least half of its hidden checks",
    )


def test_invalid_tasks_listed():
    rows = [*RESULTS, row("gemini", "005-bad", "0", status="skipped_invalid")]
    assert build_report(RUN, rows).invalid_tasks == ("005-bad",)


def test_chart_points():
    assert build_report(RUN, RESULTS).chart[0] == {
        "config": "claude",
        "cost_usd": "2.400000",
        "quality": 1.0,
        "pareto": False,
    }


def test_done_without_hidden_result_is_not_run():
    rows = [
        *trials("a", "1", [True] * 3, TASKS[:3]),
        row("a", TASKS[3], "1", status="done", passed=None),
    ]
    c = build_report(RUN, rows).configs[0]
    assert (c.completed, c.not_run) == (3, 1)
