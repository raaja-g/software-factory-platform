from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

from swfactory.cli import main
from swfactory.cli.main import app

STATUS = {
    "enabled": True,
    "suite": "v1-parity",
    "threshold": 25,
    "max_failure_age_days": 7,
    "failures": 27,
    "oldest_failure_at": "2026-11-01T00:00:00+00:00",
    "last_improvement_at": None,
    "due": "threshold",
    "in_progress": [{"role_group": "builder", "status": "benchmarking"}],
}


def _patch(monkeypatch, **by_name: Any) -> list[tuple]:
    """Stand in for the database seam; answers by the name of the query function."""
    calls: list[tuple] = []

    def fake(fn, *args):
        calls.append((fn.__name__, args))
        return by_name[fn.__name__]

    monkeypatch.setattr(main, "_improve_db", fake)
    return calls


def test_cli_status(monkeypatch):
    _patch(monkeypatch, _improve_status_data=STATUS)
    out = CliRunner().invoke(app, ["improve", "status"]).output
    assert "failures since last improvement: 27 of 25" in out
    assert "next trigger: threshold" in out and "in progress: builder (benchmarking)" in out


def test_run_passes_the_role_and_names_the_queued_improvements(monkeypatch):
    calls = _patch(
        monkeypatch, _improve_run_data={"started": [{"id": "i-1", "role_group": "builder"}]}
    )
    r = CliRunner().invoke(app, ["improve", "run", "--role", "builder"])
    assert r.exit_code == 0 and "queued improvement i-1 for builder" in r.output
    assert calls == [("_improve_run_data", ("builder",))]


def test_run_refuses_without_a_suite(monkeypatch):
    _patch(monkeypatch, _improve_run_data={"error": "set improver.bench_suite first"})
    r = CliRunner().invoke(app, ["improve", "run"])
    assert r.exit_code == 1 and "set improver.bench_suite first" in r.output


def test_run_rejects_an_unknown_role(monkeypatch):
    calls = _patch(monkeypatch, _improve_run_data={"started": []})
    r = CliRunner().invoke(app, ["improve", "run", "--role", "nope; rm -rf"])
    assert r.exit_code == 2 and calls == []


def test_run_with_nothing_to_improve(monkeypatch):
    _patch(monkeypatch, _improve_run_data={"started": []})
    assert "nothing to improve" in CliRunner().invoke(app, ["improve", "run"]).output


def test_list_default_limit_and_rows(monkeypatch):
    row = {
        "id": "i-1",
        "role_group": "builder",
        "trigger": "age",
        "status": "pr_open",
        "pr_url": "https://github.com/o/d/pull/3",
        "created_at": "2026-11-12T00:00:00+00:00",
    }
    calls = _patch(monkeypatch, _improve_list_data=[row])
    out = CliRunner().invoke(app, ["improve", "list"]).output
    assert "builder age pr_open https://github.com/o/d/pull/3" in out
    assert calls == [("_improve_list_data", (20,))]


def test_run_warns_when_this_configuration_has_no_improver_ticker(monkeypatch):
    _patch(monkeypatch, _improve_run_data={"started": [{"id": "i-1", "role_group": "builder"}]})
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(definition_source=None))
    out = CliRunner().invoke(app, ["improve", "run"]).output
    assert "no improver ticker" in out and "72 h" in out and "next improver tick" not in out
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(definition_source="/def"))
    out = CliRunner().invoke(app, ["improve", "run"]).output
    assert "next improver tick" in out and "no improver ticker" not in out
