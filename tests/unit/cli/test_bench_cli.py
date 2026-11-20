import json

import httpx
from typer.testing import CliRunner

from swfactory.cli import api_client
from swfactory.cli.main import app

SAVE = {"mode": "pr", "sha": None, "pr_url": "https://github.com/o/d/pull/2", "active_sha": None}


def test_bench_add_prints_the_result(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(path=req.url.path, body=json.loads(req.content))
        return httpx.Response(201, json={"suite": "v1", "task": "003-todo-web", "save": SAVE})

    monkeypatch.setattr(api_client, "DEFAULT_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setenv("FACTORY_API_TOKEN", "t")
    r = CliRunner().invoke(
        app,
        ["bench", "add", "--from-job", "todo/3", "--suite", "v1", "--scorer", "merge-readiness"],
    )
    assert r.exit_code == 0, r.output
    assert seen["path"] == "/api/bench/tasks"
    assert seen["body"]["job"] == "todo:3" and seen["body"]["scorers"] == ["merge-readiness"]
    assert "003-todo-web" in r.output and "pull/2" in r.output


def test_bench_add_shows_the_refusal(monkeypatch):
    def handler(req):
        return httpx.Response(422, json={"detail": "only merged jobs can become benchmark tasks"})

    monkeypatch.setattr(api_client, "DEFAULT_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setenv("FACTORY_API_TOKEN", "t")
    r = CliRunner().invoke(app, ["bench", "add", "--from-job", "todo:3", "--suite", "v1"])
    assert r.exit_code == 1 and "only merged jobs" in r.output


def _estimate_api(monkeypatch, seen):
    est = {
        "suite": "v1-parity", "configs": ["a"], "repetitions": 1, "trials": 12,
        "estimate_usd": "57.48", "max_usd": "120.48", "budget_usd": "40.00",
        "per_config": {"a": "57.48"}, "basis": {"a": "history"},
    }  # fmt: skip

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, json.loads(req.content)))
        if req.url.path == "/api/bench/estimate":
            return httpx.Response(200, json=est)
        return httpx.Response(202, json={"run_id": "r-1", "status": "running"})

    monkeypatch.setattr(api_client, "DEFAULT_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setenv("FACTORY_API_TOKEN", "t")


def test_estimate_cli(monkeypatch):
    seen: list = []
    _estimate_api(monkeypatch, seen)
    r = CliRunner().invoke(app, ["bench", "estimate", "v1-parity", "--config", "a"])
    assert r.exit_code == 0, r.output
    assert "12 trials · estimated $57.48 (up to $120.48) · budget cap $40.00" in r.output
    assert seen[0][1]["configs"] == ["a"]


def test_run_cli_asks_then_posts_the_accepted_estimate(monkeypatch):
    seen: list = []
    _estimate_api(monkeypatch, seen)
    r = CliRunner().invoke(app, ["bench", "run", "v1-parity", "--budget", "30"], input="y\n")
    assert r.exit_code == 0 and "started bench run r-1" in r.output
    path, body = seen[-1]
    assert path == "/api/bench/runs" and body["accept_estimate_usd"] == "57.48"
    assert body["budget_usd"] == "30" and "budget cap $30.00" in r.output


def test_run_cli_declined_posts_nothing(monkeypatch):
    seen: list = []
    _estimate_api(monkeypatch, seen)
    r = CliRunner().invoke(app, ["bench", "run", "v1-parity"], input="n\n")
    assert r.exit_code == 1 and [p for p, _ in seen] == ["/api/bench/estimate"]


def test_run_cli_yes_skips_the_question(monkeypatch):
    seen: list = []
    _estimate_api(monkeypatch, seen)
    r = CliRunner().invoke(app, ["bench", "run", "v1-parity", "--yes", "--definition", "abc"])
    assert r.exit_code == 0 and seen[-1][1]["definition_sha"] == "abc"
