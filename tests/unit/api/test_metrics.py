from decimal import Decimal

from tests.unit.api.test_access import app_server, client_as  # noqa: F401
from tests.unit.api.test_actions_auth import client_viewer  # noqa: F401

from swfactory.api.routes import metrics as metrics_routes
from swfactory.api.routes.board import board_job
from swfactory.scoring.calibration import Calibration
from swfactory.scoring.metrics import MetricJob, MetricScore, MetricsRows, summarize_metrics

SC, GEM, RS, JS = "merge-readiness", "gem@v", "r1", "j1"


def obj(passed, role="builder", model="opus", engine="claude_code", profile="default"):
    return MetricScore("obj.gate", passed, role, model, engine, profile)


def judge(passed, rub=RS, sha=JS):
    return MetricScore(SC, passed, "judge", GEM, "claude_code", "default", GEM, rub, sha)


def cal(n=3, calibrated=False, k=0.5):
    return {(SC, GEM, RS, JS): Calibration(SC, GEM, RS, JS, n, n, 0, k, calibrated)}


SCORES = [obj(True), obj(True), obj(True), obj(False), obj(True, role="reviewer")]
ROWS = MetricsRows(scores=[*SCORES, judge(True)], calibration=cal())


def m(jobs=(), *, waited=0, finished=None, runs=0, swapped=0):
    js = [
        MetricJob(st, Decimal(c), waited=i < waited)
        for i, (st, c, *kind) in enumerate(jobs)
        if not kind
    ]
    while finished is not None and len(js) < finished:
        js.append(MetricJob("closed", Decimal(0), waited=len(js) < waited))
    return summarize_metrics(MetricsRows(jobs=js, runs=runs, fallbacks=swapped), window_days=30)


def test_pass_rate_by_role():
    r = summarize_metrics(ROWS, window_days=30)["pass_rate"]["role"]
    assert r[0] == {"key": "builder", "n": 4, "pass_rate": 0.75}


def test_uncalibrated_judges_out_of_pass_rates():
    bad = MetricsRows(scores=[*SCORES, judge(True), judge(False)], calibration=cal())
    assert (
        summarize_metrics(bad, window_days=30)["pass_rate"]
        == summarize_metrics(ROWS, window_days=30)["pass_rate"]
    )


def test_calibrated_judges_count():
    rows = MetricsRows(scores=[judge(True), judge(False)], calibration=cal(30, True, 0.7))
    assert summarize_metrics(rows, window_days=30)["pass_rate"]["model"] == [
        {"key": GEM, "n": 2, "pass_rate": 0.5}
    ]


def test_scorer_rows_flag_calibration():
    r = summarize_metrics(ROWS, window_days=30)["scorers"][0]
    assert (r["scorer"], r["calibrated"], r["labels"]) == ("merge-readiness", False, 3)
    assert (r["judge_model"], r["n"], r["pass_rate"], r["kappa"]) == (GEM, 1, 1.0, 0.5)


def test_cost_per_merged_counts_all_spend():
    got = m(jobs=[("merged", "2"), ("merged", "3"), ("closed", "1")])
    assert got["cost_per_merged_usd"] == "3.000000"


def test_cost_per_merged_none_without_merges():
    assert m(jobs=[("closed", "1")])["cost_per_merged_usd"] is None


def test_escalation_and_fallback_rates():
    r = m(waited=1, finished=4, runs=10, swapped=1)
    assert (r["escalation_rate"], r["fallback_rate"]) == (0.25, 0.1)


def test_rounds_histogram():
    rows = MetricsRows(
        jobs=[MetricJob("merged", Decimal(1), 1), MetricJob("merged", Decimal(1), 3)]
    )
    r = summarize_metrics(rows, window_days=7)
    assert r["rounds"] == {
        "mean": 2.0,
        "histogram": [{"rounds": 1, "jobs": 1}, {"rounds": 3, "jobs": 1}],
    }


def test_benchmark_jobs_excluded():
    assert m(jobs=[("closed", "9", "benchmark")])["jobs"] == 0


def test_metrics_route(client_viewer, monkeypatch):  # noqa: F811
    seen = {}

    async def rows(s, *, app_slug, since, app_slugs=None):
        seen.update(app=app_slug, slugs=app_slugs)
        return MetricsRows()

    monkeypatch.setattr(metrics_routes, "metrics_rows", rows)
    j = client_viewer.get("/api/metrics").json()
    assert j["available"] is True and set(j["pass_rate"]) == {"role", "model", "engine", "profile"}
    assert j["window_days"] == 30 and seen == {"app": None, "slugs": None}


def test_metrics_skip_hidden_apps(client_as, monkeypatch):  # noqa: F811
    async def rows(s, *, app_slug, since, app_slugs=None):
        assert app_slugs == frozenset({"web"})
        return MetricsRows()

    monkeypatch.setattr(metrics_routes, "metrics_rows", rows)
    c = client_as("bob@x.io", "viewer")
    assert c.get("/api/metrics?app=payments").status_code == 404
    assert c.get("/api/metrics").status_code == 200


BASE = {
    "job_id": "7c1d0b0e-0000-4000-8000-000000000001", "seq": 3, "slug": "todo-web",
    "app": "todo", "title": "t", "updated": "2026-01-01T00:00:00Z",
}  # fmt: skip
Q = {"badge": "good", "judges_passed": 1, "judges_total": 2}
BUILDING = BASE | {"stage": "build", "quality": Q}
MERGED = BASE | {"stage": "merged", "quality": Q}


def test_board_quality_only_done():
    assert board_job(BUILDING)["quality"] is None and board_job(MERGED)["quality"] == Q
    assert board_job(BASE | {"stage": "merged"})["quality"] is None
