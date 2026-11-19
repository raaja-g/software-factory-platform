import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from swfactory.cli import api_client
from swfactory.cli.main import app
from swfactory.settings import get_settings

SHOWN_HEAD = "a" * 40
FLASH = {
    "alias": "flash@vertex", "provider": "vertex", "upstream_id": "gemini-flash",
    "strength": "cheap", "price_in_per_mtok": "0.1", "price_out_per_mtok": "0.4",
    "context_window": 1000, "supports_tools": True, "messages_tools_ok": True,
    "available": True, "error_class": None, "latency_ms": 50, "last_error": None,
}  # fmt: skip
JOB = {
    "job_id": "11111111-1111-1111-1111-111111111111", "ref": "todo:001", "app": "todo",
    "title": "Add x", "stage": "needs_human", "round": 1, "decision": None,
    "cost_usd": "0.100000", "budget_usd": "5.00", "created_at": "2026-01-01T00:00:00+00:00",
    "waiting": {"head_sha": SHOWN_HEAD},
}  # fmt: skip
BACKLOG = {
    "ref": "todo:b001", "status": "paused", "title": "b.md", "jobs_merged": 1, "jobs_total": 2,
    "blocked_job": "todo:002",
    "items": [
        {"pos": 1, "job_ref": "todo:001", "stage": "merged", "cost_usd": "0.1", "title": "a"},
        {"pos": 2, "job_ref": "todo:002", "stage": "failed", "cost_usd": "0.2", "title": "b"},
    ],
}  # fmt: skip


STARTED_RUN = {
    "status": "started", "app": "todo-app", "backlog": "todo-app:b001",
    "job_ref": "todo-app:001", "detail": None,
}  # fmt: skip


class MockApi:
    def __init__(self, fail: tuple[int, str] | None = None):
        self.paths: list[str] = []
        self.bodies: list[dict] = []
        self.headers: dict[str, str] = {}
        self.fail = fail
        self.polls = 0
        self.run_states: list[dict] = [STARTED_RUN]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.headers = dict(request.headers)
        self.paths.append(path)
        body = json.loads(request.content) if request.content else None
        if body is not None:
            self.bodies.append(body)
        if self.fail and request.method == "POST" and path.endswith("/actions"):
            return httpx.Response(self.fail[0], json={"detail": self.fail[1]})
        if path == "/api/runs" and request.method == "POST":
            return httpx.Response(202, json={"request_id": "r1"})
        if path == "/api/runs/r1":
            state = self.run_states[0] if len(self.run_states) == 1 else self.run_states.pop(0)
            return httpx.Response(200, json=state)
        if path == "/api/jobs" and request.method == "POST":
            return httpx.Response(201, json={"job_id": JOB["job_id"], "ref": "todo:001"})
        if path == "/api/jobs":
            return httpx.Response(200, json=[JOB])
        if path.endswith("/runs"):
            return httpx.Response(200, json=[])
        if path.startswith("/api/jobs/") and path.endswith("/actions"):
            return httpx.Response(202, json={"command_id": "c"})
        if path.startswith("/api/jobs/"):
            return httpx.Response(200, json=JOB)
        if path == "/api/backlogs/import":
            return httpx.Response(201, json={"ref": "todo:b001"})
        if path == "/api/backlogs":
            return httpx.Response(200, json=[BACKLOG])
        if path.endswith("/actions"):
            return httpx.Response(200, json={"status": "running"})
        if path.startswith("/api/backlogs/"):
            return httpx.Response(200, json=BACKLOG)
        if path == "/api/models/refresh":
            return httpx.Response(202, json={"request_id": 7})
        if path == "/api/models/refresh/7":
            self.polls += 1
            if self.polls == 1:
                return httpx.Response(200, json={"done": False})
            return httpx.Response(200, json={"done": True, "models": [FLASH]})
        if path == "/api/models":
            return httpx.Response(200, json=[FLASH])
        if path == "/api/apps":
            return httpx.Response(
                200,
                json=[{"slug": "todo", "repo_kind": "local", "merge_policy": None, "profile": "p"}],
            )
        if path == "/api/login-code":
            return httpx.Response(200, json={"code": "abc"})
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def env_token(monkeypatch):
    monkeypatch.setenv("FACTORY_API_TOKEN", "sfp_t")
    get_settings.cache_clear()


def _install(monkeypatch, mock: MockApi):
    monkeypatch.setattr(api_client, "DEFAULT_TRANSPORT", httpx.MockTransport(mock.handle))
    monkeypatch.setattr(api_client.time, "sleep", lambda s: None)
    return mock


@pytest.fixture
def mock_api(monkeypatch, env_token):
    return _install(monkeypatch, MockApi())


@pytest.fixture
def mock_api_409(monkeypatch, env_token):
    return _install(
        monkeypatch, MockApi((409, "job todo:002 is failed; skip it or cancel the backlog"))
    )


@pytest.fixture
def mock_api_failed(monkeypatch, env_token):
    mock = MockApi()
    mock.run_states = [
        {"status": "failed", "app": None, "backlog": None, "job_ref": None,
         "detail": "'/x' is not a git repository"}
    ]  # fmt: skip
    return _install(monkeypatch, mock)


@pytest.fixture
def mock_api_waiting_then_started(monkeypatch, env_token):
    mock = MockApi()
    pr = "https://github.com/org/def/pull/3"
    waiting = {"status": "waiting", "app": None, "backlog": None, "job_ref": None, "detail": pr}
    mock.run_states = [waiting, waiting, STARTED_RUN]
    return _install(monkeypatch, mock)


@pytest.fixture
def mock_api_403(monkeypatch, env_token):
    return _install(monkeypatch, MockApi((403, "approver role required for app 'todo'")))


@pytest.fixture
def tmp_token_file(monkeypatch, tmp_path: Path):
    f = tmp_path / "tok"
    f.write_text("sfp_fromfile\n")
    monkeypatch.delenv("FACTORY_API_TOKEN", raising=False)
    monkeypatch.setenv("FACTORY_API_TOKEN_FILE", str(f))
    get_settings.cache_clear()
    return f


@pytest.fixture
def env_no_mode(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("FACTORY_MODE", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def backlog_file(tmp_path, monkeypatch):
    (tmp_path / "b.md").write_text("# Backlog\n- [x] done\n- [ ] a\n")
    monkeypatch.chdir(tmp_path)


def run(*args: str, input: str | None = None):
    return CliRunner().invoke(app, list(args), input=input)
