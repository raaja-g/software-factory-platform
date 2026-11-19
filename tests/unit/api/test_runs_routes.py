import dataclasses
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.unit.api.api_support import FakeDefinitions, FakeKeys, fake_deps, server

from swfactory.api.app import create_app
from swfactory.api.auth import hash_token
from swfactory.api.routes import runs as runs_routes
from swfactory.apps import RegisterError

ROLES = {"submitter@x.io": "submitter", "viewer@x.io": "viewer", "admin@x.io": "admin"}


class Users:
    async def role_of(self, email):
        return ROLES.get(email)

    async def token_email(self, token_hash):
        return next((e for e in ROLES if hash_token("sfp_" + e) == token_hash), None)


class _Tx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *a):
        return False


class _Sessions:
    def begin(self):
        return _Tx()

    def __call__(self):
        return _Tx()


class Defs(FakeDefinitions):
    def __init__(self):
        super().__init__()
        todo = SimpleNamespace(
            repo_kind="local", local_path="/Users/me/work/todo-app", repo_url="todo-app",
            access=None,
        )  # fmt: skip
        budgets = SimpleNamespace(job_max_usd=25)
        self.defn = SimpleNamespace(
            factory=SimpleNamespace(apps={"todo-app": todo}, budgets=budgets)
        )

    async def active(self):
        return "sha", self.defn


class FakeRuns:
    def __init__(self):
        self.created: list[dict] = []
        self.ids: list[str] = []
        self.started_id = str(uuid4())
        self.owner = "admin@x.io"

    async def create(self, s, **kw):
        self.created.append(kw)
        self.ids.append(str(uuid4()))
        return self.ids[-1]

    async def view(self, s, rid):
        if str(rid) != self.started_id:
            return None
        return {
            "status": "started", "app": "todo-app", "backlog": "todo-app:b001",
            "job_ref": "todo-app:001", "detail": None, "requested_by": self.owner,
        }  # fmt: skip


@pytest.fixture
def fake_runs(monkeypatch):
    f = FakeRuns()
    monkeypatch.setattr(runs_routes, "create_run_request", f.create)
    monkeypatch.setattr(runs_routes, "get_run_view", f.view)
    return f


@pytest.fixture
def app_(fake_runs):
    deps = dataclasses.replace(
        fake_deps(settings_()),
        sessions=_Sessions(),  # pyright: ignore[reportArgumentType]
        definitions=Defs(),  # pyright: ignore[reportArgumentType]
        users=Users(),  # pyright: ignore[reportArgumentType]
    )
    return create_app(settings_(), deps, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]


def settings_():
    return server(FACTORY_HOST_HOME="/Users/me")


def client(app_, email=None):
    headers = {"authorization": f"Bearer sfp_{email}"} if email else {}
    return TestClient(app_, base_url="http://127.0.0.1:8080", headers=headers)


@pytest.fixture
def client_admin(app_):
    return client(app_, "admin@x.io")


@pytest.fixture
def client_submitter(app_):
    return client(app_, "submitter@x.io")


@pytest.fixture
def client_anonymous(app_):
    return client(app_)


def test_post_run_for_a_registered_repo_needs_submitter(client_submitter, fake_runs):
    r = client_submitter.post(
        "/api/runs", json={"repo": "~/work/todo-app", "requirement": "A todo list"}
    )
    assert (r.status_code, r.json()) == (202, {"request_id": fake_runs.ids[-1]})
    assert fake_runs.created[-1] == {
        "repo": "/Users/me/work/todo-app", "requirement": "A todo list", "profile": None,
        "ci_safe": False, "budget_usd": None, "requested_by": "submitter@x.io",
    }  # fmt: skip


def test_registering_a_new_repo_needs_admin(client_submitter, client_admin, fake_runs):
    body = {"repo": "acme/web", "requirement": "x"}
    r = client_submitter.post("/api/runs", json=body)
    assert r.status_code == 403
    assert r.json()["detail"] == "registering a repo needs the admin role"
    assert fake_runs.created == []
    assert client_admin.post("/api/runs", json=body).status_code == 202


def test_a_viewer_cannot_post(app_, fake_runs):
    r = client(app_, "viewer@x.io").post("/api/runs", json={"repo": "acme/web", "requirement": "x"})
    assert r.status_code == 403 and fake_runs.created == []


@pytest.mark.parametrize(
    "body, status",
    [
        ({"repo": "work/app", "requirement": "x"}, 422),
        ({"repo": "acme/web", "requirement": "  "}, 422),
        ({"repo": "acme/web", "requirement": "x" * 200_001}, 413),
        ({"repo": "acme/web", "requirement": "x", "profile": "Bad Name"}, 422),
        ({"repo": "acme/web", "requirement": "x", "budget_usd": "26"}, 422),
        ({"repo": "acme/web", "requirement": "x", "budget_usd": "5"}, 202),
    ],
)
def test_post_run_validation(client_admin, body, status):
    assert client_admin.post("/api/runs", json=body).status_code == status


def test_a_create_error_is_a_422(client_admin, monkeypatch):
    async def boom(s, **kw):
        raise RegisterError("nope")

    monkeypatch.setattr(runs_routes, "create_run_request", boom)
    assert (
        client_admin.post("/api/runs", json={"repo": "acme/web", "requirement": "x"}).status_code
        == 422
    )


def test_post_run_needs_a_token(client_anonymous):
    r = client_anonymous.post("/api/runs", json={"repo": "acme/web", "requirement": "x"})
    assert r.status_code == 401


def test_get_run_shape(client_admin, fake_runs):
    r = client_admin.get(f"/api/runs/{fake_runs.started_id}")
    assert r.json() == {
        "status": "started", "app": "todo-app", "backlog": "todo-app:b001",
        "job_ref": "todo-app:001", "detail": None,
    }  # fmt: skip


def test_get_run_of_someone_else_is_404_unless_admin(app_, fake_runs):
    assert (
        client(app_, "submitter@x.io").get(f"/api/runs/{fake_runs.started_id}").status_code == 404
    )
    assert client(app_, "admin@x.io").get(f"/api/runs/{fake_runs.started_id}").status_code == 200
    assert client(app_, "admin@x.io").get("/api/runs/not-a-uuid").status_code == 404
