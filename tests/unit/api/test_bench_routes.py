from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.unit.api.test_actions_auth import (  # noqa: F401
    client_admin,
    client_approver,
)

from swfactory.api.routes import bench as bench_routes
from swfactory.bench.tasks import BenchTaskDraft, BenchTaskError
from swfactory.cli.api_client import ApiClient, ApiError
from swfactory.definition.writer import DefinitionWriteError


class Writer:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def save_files(self, files, *, message, author):
        if self.error:
            raise self.error
        self.calls.append((dict(files), message, author))
        return SimpleNamespace(mode="pr", sha=None, pr_url="https://github.com/o/d/pull/2")


@pytest.fixture
def bench(client_admin, monkeypatch):  # noqa: F811
    seen = {}

    async def fake_task(s, job, **kw):
        seen.update(job=job, **{k: kw[k] for k in ("suite", "name", "scorers", "check_command")})
        if job == "todo:999":
            raise BenchTaskError("only merged jobs can become benchmark tasks")
        return BenchTaskDraft("v1", "003-todo-web", {"benchmarks/v1/suite.yaml": "a: 1\n"})

    monkeypatch.setattr(bench_routes, "task_from_job", fake_task)
    writer = Writer()
    app = client_admin.app
    app.state.deps = replace(app.state.deps, writer=writer)  # pyright: ignore[reportAttributeAccessIssue]
    return SimpleNamespace(client=client_admin, writer=writer, seen=seen, app=app)


def test_bench_add_needs_admin(client_approver):  # noqa: F811
    r = client_approver.post("/api/bench/tasks", json={"job": "todo:003", "suite": "v1"})
    assert r.status_code == 403


def test_bench_add_saves_and_returns_201(bench):
    r = bench.client.post(
        "/api/bench/tasks",
        json={"job": "todo:003", "suite": "v1", "scorers": ["merge-readiness"]},
    )
    assert r.status_code == 201
    body = r.json()
    assert (body["suite"], body["task"]) == ("v1", "003-todo-web")
    assert body["save"]["mode"] == "pr" and body["save"]["pr_url"].endswith("/pull/2")
    files, message, author = bench.writer.calls[0]
    assert message == "bench: add 003-todo-web from job todo:003" and author == "admin@x.io"
    assert bench.seen["scorers"] == ["merge-readiness"]


def test_bench_add_refusal_is_422(bench):
    r = bench.client.post("/api/bench/tasks", json={"job": "todo:999", "suite": "v1"})
    assert r.status_code == 422 and "only merged jobs" in r.json()["detail"]


def test_bench_add_writer_error_is_409(bench):
    bench.app.state.deps = replace(
        bench.app.state.deps, writer=Writer(DefinitionWriteError("refusing"))
    )
    r = bench.client.post("/api/bench/tasks", json={"job": "todo:003", "suite": "v1"})
    assert r.status_code == 409


def test_cli_client_round_trip_against_the_real_route(bench):
    c = ApiClient("http://127.0.0.1:8080", "sfp_d")
    c._http = bench.client  # the real app behind the real client methods  # pyright: ignore[reportAttributeAccessIssue]
    out = c.add_bench_task("todo:003", "v1", scorers=["merge-readiness"])
    assert out["task"] == "003-todo-web" and out["save"]["mode"] == "pr"
    with pytest.raises(ApiError, match="only merged jobs"):
        c.add_bench_task("todo:999", "v1")


# --- M10.13: estimate, start and cancel ----------------------------------------------------------

from decimal import Decimal  # noqa: E402

from swfactory.bench.runner import Estimate, EstimateChanged  # noqa: E402
from swfactory.definition.models import BenchSuiteDef  # noqa: E402

EST = Estimate(
    12, Decimal("57.48"), Decimal("120.48"), {"gemini": Decimal("30")}, {"gemini": "history"}
)
SUITE = BenchSuiteDef.model_construct(budget_usd=Decimal("40"))


@pytest.fixture
def runs(client_admin, monkeypatch):  # noqa: F811
    seen = {}

    async def fake_estimate(s, defn, **kw):
        seen["estimate"] = kw
        if kw["suite"] == "nope":
            raise ValueError("no such benchmark suite 'nope'")
        return SUITE, [SimpleNamespace(name="gemini")], EST

    async def fake_start(s, defn, **kw):
        seen["start"] = kw
        if kw["accept_estimate_usd"] != EST.total_usd:
            raise EstimateChanged(EST.total_usd, kw["accept_estimate_usd"])
        return "11111111-1111-1111-1111-111111111111"

    monkeypatch.setattr(bench_routes, "compute_estimate", fake_estimate)
    monkeypatch.setattr(bench_routes, "start_bench_run", fake_start)
    return SimpleNamespace(client=client_admin, seen=seen)


def test_estimate_route_shape(runs):
    r = runs.client.post("/api/bench/estimate", json={"suite": "v1-parity", "repetitions": 2})
    assert r.status_code == 200
    body = r.json()
    assert (body["trials"], body["estimate_usd"], body["max_usd"], body["budget_usd"]) == (
        12, "57.48", "120.48", "40",
    )  # fmt: skip
    assert runs.seen["estimate"]["repetitions"] == 2


def test_estimate_unknown_suite_is_422(runs):
    assert runs.client.post("/api/bench/estimate", json={"suite": "nope"}).status_code == 422


def test_bench_runs_need_admin(client_approver):  # noqa: F811
    assert client_approver.post("/api/bench/estimate", json={"suite": "s"}).status_code == 403
    body = {"suite": "s", "accept_estimate_usd": "1"}
    assert client_approver.post("/api/bench/runs", json=body).status_code == 403
    assert client_approver.post("/api/bench/runs/x/cancel").status_code == 403


def test_run_route_starts_with_202(runs):
    r = runs.client.post(
        "/api/bench/runs",
        json={"suite": "v1-parity", "accept_estimate_usd": "57.48", "budget_usd": "40"},
    )
    assert r.status_code == 202 and r.json()["run_id"].startswith("1111")
    assert runs.seen["start"]["budget_usd"] == Decimal("40")
    assert runs.seen["start"]["definition_sha"] == "sha"


def test_run_route_conflict_on_changed_estimate(runs):
    r = runs.client.post(
        "/api/bench/runs", json={"suite": "v1-parity", "accept_estimate_usd": "1.00"}
    )
    assert r.status_code == 409 and "57.48" in r.json()["detail"]


def test_cli_client_round_trip_estimate_and_run(runs):
    c = ApiClient("http://127.0.0.1:8080", "sfp_d")
    c._http = runs.client  # pyright: ignore[reportAttributeAccessIssue]
    est = c.bench_estimate("v1-parity", configs=["gemini"], repetitions=2)
    assert est["trials"] == 12 and runs.seen["estimate"]["configs"] == ["gemini"]
    out = c.bench_run("v1-parity", accept_estimate_usd=est["estimate_usd"], budget=Decimal("40"))
    assert out["run_id"].startswith("1111")
    with pytest.raises(ApiError) as e:
        c.bench_run("v1-parity", accept_estimate_usd="1.00")
    assert e.value.status == 409


# --- M10.14: suites, runs and the report ----------------------------------------------------------

from uuid import UUID  # noqa: E402

from tests.unit.api.test_access import app_server, client_as  # noqa: E402, F401
from typer.testing import CliRunner  # noqa: E402

from swfactory.cli.main import app as cli_app  # noqa: E402

RUN_ID = "00000000-0000-0000-0000-0000000000a1"
RUN_PAYMENTS_ID = "00000000-0000-0000-0000-0000000000b2"
TASKS = ("001-bootstrap-python", "002-todo-cli", "003-todo-web", "004-todo-api")


def _task(repo):
    return SimpleNamespace(repo=repo)


def _suite(repo):
    return SimpleNamespace(
        description="d",
        budget_usd=Decimal("40"),
        configurations=[SimpleNamespace(name=n) for n in ("gemini", "claude", "economy")],
        tasks={t: _task(repo) for t in TASKS},
    )


def _result(config, task, cost, passed=True):
    return {
        "configuration": config, "task": task, "status": "done", "hidden_passed": passed,
        "judge_pass": None, "judge_pass_uncalibrated": None, "rounds": 2,
        "cost_usd": Decimal(cost), "duration_s": Decimal("60"), "hidden_detail": None,
    }  # fmt: skip


def _run(rid, suite):
    return {
        "id": UUID(rid), "suite": suite, "status": "done", "definition_sha": "a" * 40,
        "budget_usd": Decimal("40"), "cost_usd": Decimal("20"), "estimate_usd": None,
        "repetitions": 1, "requested_by": "a@x.io", "created_at": None, "finished_at": None,
    }  # fmt: skip


def _results():
    return [
        _result(c, t, cost, passed=not (c == "economy" and t == TASKS[3]))
        for c, cost in (("gemini", "1.20"), ("claude", "2.40"), ("economy", "0.50"))
        for t in TASKS
    ]


class BenchDefs:
    async def active(self):
        apps = {"web": SimpleNamespace(access=None), "payments": SimpleNamespace(access=ACC_P)}
        fac = SimpleNamespace(apps=apps)
        return "sha", SimpleNamespace(
            factory=fac,
            benchmarks={"v1-parity": _suite("local:web"), "pay": _suite("local:payments")},
        )


from tests.unit.api.test_access import ACC as ACC_P  # noqa: E402


@pytest.fixture
def reports(client_as, monkeypatch):  # noqa: F811
    data = {
        RUN_ID: (_run(RUN_ID, "v1-parity"), _results()),
        RUN_PAYMENTS_ID: (_run(RUN_PAYMENTS_ID, "pay"), _results()),
    }

    async def load_runs(s):
        return list(data.values())

    async def load_run(s, rid):
        return data.get(str(rid))

    monkeypatch.setattr(bench_routes, "load_runs", load_runs)
    monkeypatch.setattr(bench_routes, "load_run", load_run)

    def make(email="x@x.io", role="viewer"):
        c = client_as(email, role)
        c.app.state.deps = replace(c.app.state.deps, definitions=BenchDefs())  # pyright: ignore[reportAttributeAccessIssue]
        return c

    return make


def test_report_route(reports):
    body = reports().get(f"/api/bench/runs/{RUN_ID}").json()
    assert body["recommended"] == "gemini" and body["id"] == RUN_ID
    assert body["configs"][0]["hidden_pass_interval"] is not None


def test_report_hidden_app_is_404(reports):
    c = reports("bob@x.io", "viewer")
    assert c.get(f"/api/bench/runs/{RUN_PAYMENTS_ID}").status_code == 404
    assert RUN_PAYMENTS_ID not in [r["id"] for r in c.get("/api/bench/runs").json()]
    assert [s["name"] for s in c.get("/api/bench/suites").json()] == ["v1-parity"]
    assert c.get(f"/api/bench/runs/{RUN_ID}").status_code == 200


def test_listed_user_sees_the_payments_run(reports):
    c = reports("ana@x.io", "approver")
    assert {r["id"] for r in c.get("/api/bench/runs").json()} == {RUN_ID, RUN_PAYMENTS_ID}
    assert [s["name"] for s in c.get("/api/bench/suites").json()] == ["pay", "v1-parity"]
    s = c.get("/api/bench/suites").json()[1]
    assert (s["tasks"], s["configurations"], s["budget_usd"]) == (
        4, ["gemini", "claude", "economy"], "40",
    )  # fmt: skip


def test_unknown_run_is_404(reports):
    assert reports().get("/api/bench/runs/not-a-uuid").status_code == 404
    assert reports().get(f"/api/bench/runs/{UUID(int=7)}").status_code == 404


def test_cli_report_round_trip_against_the_real_route(reports, monkeypatch):
    c = reports()
    cl = ApiClient("http://127.0.0.1:8080", "sfp_d")
    cl._http = c  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr("swfactory.cli.main._client", lambda: cl)
    r = CliRunner().invoke(cli_app, ["bench", "report", RUN_ID])
    assert r.exit_code == 0, r.output
    assert "Recommended: gemini: quality 1.00 at $1.20 per task" in r.output
    assert "economy" in r.output and "0.75 (n=4, 0.30-0.95)" in r.output
    j = CliRunner().invoke(cli_app, ["bench", "report", RUN_ID, "--json"])
    assert '"recommended": "gemini"' in j.output
    lst = CliRunner().invoke(cli_app, ["bench", "list"])
    assert lst.exit_code == 0 and "v1-parity" in lst.output and RUN_ID in lst.output
