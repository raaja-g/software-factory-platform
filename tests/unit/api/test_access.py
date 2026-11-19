from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.unit.api.api_support import FakeKeys, server

from swfactory.api import access as access_mod
from swfactory.api.access import app_role
from swfactory.api.app import ApiDeps, create_app
from swfactory.api.auth import hash_token
from swfactory.api.routes import apps as apps_routes
from swfactory.api.routes import board as board_routes
from swfactory.api.routes import jobs as jobs_routes
from swfactory.definition.models import AppAccess
from swfactory.domain.types import FactoryMode

ACC = AppAccess(viewers=("v@x.io",), submitters=("s@x.io",), approvers=("ana@x.io",))
JOB_ID = uuid4()


def test_unset_access_keeps_instance_role():
    assert app_role("approver", "bob@x.io", None, mode=FactoryMode.SERVER) == "approver"


@pytest.mark.parametrize(
    "role, email, want",
    [
        ("approver", "ana@x.io", "approver"),
        ("admin", "ana@x.io", "admin"),
        ("approver", "s@x.io", "submitter"),
        ("viewer", "s@x.io", "viewer"),
        ("admin", "v@x.io", "viewer"),
        ("admin", "bob@x.io", None),
    ],
)
def test_app_role_is_the_lower_of_role_and_list(role, email, want):
    assert app_role(role, email, ACC, mode=FactoryMode.SERVER) == want


def test_personal_mode_ignores_lists():
    assert app_role("admin", "local", ACC, mode=FactoryMode.PERSONAL) == "admin"


def test_access_emails_normalized():
    assert AppAccess(viewers=(" Ana@X.io",)).viewers == ("ana@x.io",)


def test_highest_list_wins():
    both = AppAccess(viewers=("a@x.io",), approvers=("a@x.io",))
    assert app_role("approver", "a@x.io", both, mode=FactoryMode.SERVER) == "approver"


class Users:
    def __init__(self):
        self.who: tuple[str, str] = ("x@x.io", "viewer")

    async def role_of(self, email):
        return self.who[1]

    async def token_email(self, token_hash):
        return self.who[0]


class Definitions:
    async def active(self):
        base = dict(
            budget_usd=None, repo_kind="local", default_branch="main", profile=None,
            merge_policy=None, max_rounds=None,
        )  # fmt: skip
        web = SimpleNamespace(access=None, **base)
        pay = SimpleNamespace(access=ACC, **base)
        budgets = SimpleNamespace(job_usd=Decimal("10"), job_max_usd=Decimal("25"))
        defaults = SimpleNamespace(profile="p", max_rounds=2)
        apps = {"web": web, "payments": pay}
        fac = SimpleNamespace(apps=apps, budgets=budgets, defaults=defaults)
        return "sha", SimpleNamespace(factory=fac)


class Sessions:
    def __call__(self):
        return self._ctx()

    @asynccontextmanager
    async def _ctx(self):
        yield SimpleNamespace()

    def begin(self):
        return self._ctx()


class FakeCommands:
    def __init__(self):
        self.calls = []

    async def __call__(self, s, **kw):
        self.calls.append(kw)
        return uuid4()


class FakeBus:
    def __init__(self):
        self.args = None

    async def subscribe(self, *, app_slug, after_id, app_slugs=None):
        self.args = {"app_slug": app_slug, "after_id": after_id, "app_slugs": app_slugs}
        return
        yield


class Events(FakeBus):
    async def emit(self, job_id, kind, data):
        pass


def _app_server(users=None, bus=None):
    s = server()
    deps = ApiDeps(
        sessions=Sessions(),  # pyright: ignore[reportArgumentType]
        events=bus or Events(),  # pyright: ignore[reportArgumentType]
        artifacts=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        catalog=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        definitions=Definitions(),  # pyright: ignore[reportArgumentType]
        users=users or Users(),  # pyright: ignore[reportArgumentType]
        writer=None,
        settings=s,
    )
    return create_app(s, deps, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]


@pytest.fixture
def app_server():
    return _app_server()


@pytest.fixture
def fake_bus():
    return FakeBus()


@pytest.fixture
def fake_commands(monkeypatch):
    fake = FakeCommands()

    async def resolve(s, ref):
        app = ref.split(":")[0]
        return SimpleNamespace(id=JOB_ID, requested_by="x@x.io", app=app)

    async def slug_of(s, job):
        return job.app

    async def list_jobs(s, **kw):
        slugs = kw["app_slugs"]
        return [
            SimpleNamespace(id=uuid4(), app_id=a, **_JOB)
            for a in ("web", "payments")
            if slugs is None or a in slugs
        ]

    monkeypatch.setattr(jobs_routes, "submit_command", fake)
    monkeypatch.setattr(access_mod, "resolve_job_ref", resolve)
    monkeypatch.setattr(access_mod, "app_slug_of", slug_of)
    monkeypatch.setattr(jobs_routes, "list_jobs", list_jobs)
    return fake


@pytest.fixture
def client_as(app_server):
    def make(email, role, srv=None):
        users = Users()
        users.who = (email, role)
        c = TestClient(srv or _app_server(users), base_url="http://127.0.0.1:8080")
        c.headers["authorization"] = "Bearer sfp_t"
        return c

    return make


def test_unlisted_approver_cannot_see_or_approve(client_as, fake_commands):
    c = client_as("bob@x.io", "approver")
    assert c.post("/api/jobs/payments:1/actions", json={"action": "approve"}).status_code == 404
    assert c.get("/api/jobs/payments:1").status_code == 404 and fake_commands.calls == []


def test_listed_submitter_cannot_approve(client_as, fake_commands):
    r = client_as("s@x.io", "approver").post(
        "/api/jobs/payments:1/actions", json={"action": "approve"}
    )
    assert (r.status_code, r.json()["detail"]) == (
        403,
        "approver role required for app 'payments'",
    )
    assert fake_commands.calls == []


def test_listed_approver_can_approve(client_as, fake_commands):
    r = client_as("ana@x.io", "approver").post(
        "/api/jobs/payments:1/actions", json={"action": "approve"}
    )
    assert r.status_code == 202


_JOB: dict = dict(
    seq=1, title="t", stage="queued", kind="normal", round=0, branch=None, decision=None,
    gate_reason=None, pr_url=None, cost_usd=Decimal(0), budget_usd=Decimal(10),
    requested_by="x", source="api",
)  # fmt: skip


def test_jobs_list_hides_unlisted_apps(fake_commands, app_server):
    from datetime import UTC, datetime

    _JOB.update(created_at=datetime.now(UTC), updated_at=datetime.now(UTC))

    class S(Sessions):
        @asynccontextmanager
        async def _ctx(self):
            yield SimpleNamespace(scalars=_scalars)

    async def _scalars(q):
        rows = [
            SimpleNamespace(id="web", slug="web"),
            SimpleNamespace(id="payments", slug="payments"),
        ]
        return SimpleNamespace(all=lambda: rows)

    object.__setattr__(app_server.state.deps, "sessions", S())
    c = _viewer_client(app_server)
    assert {j["app"] for j in c.get("/api/jobs").json()} == {"web"}
    assert c.get("/api/jobs?app=payments").json() == []


def _viewer_client(srv, email="bob@x.io", role="viewer"):
    users = Users()
    users.who = (email, role)
    object.__setattr__(srv.state.deps, "users", users)
    c = TestClient(srv, base_url="http://127.0.0.1:8080")
    c.headers["authorization"] = "Bearer sfp_t"
    return c


def test_board_passes_visible_apps(monkeypatch, app_server):
    seen = {}

    async def build(s, *, app_slug, app_slugs=None, **kw):
        seen["slugs"] = app_slugs
        return {"jobs": [], "apps": [], "last_event_id": 0, "generated_at": ""}

    class S(Sessions):  # no database: the route only asks which apps exist
        @asynccontextmanager
        async def _ctx(self):
            yield SimpleNamespace(scalars=_scalars)

    async def _scalars(q):
        rows = [
            SimpleNamespace(id="web", slug="web"),
            SimpleNamespace(id="payments", slug="payments"),
        ]
        return SimpleNamespace(all=lambda: rows)

    object.__setattr__(app_server.state.deps, "sessions", S())
    monkeypatch.setattr(board_routes, "build_board", build)
    assert _viewer_client(app_server).get("/api/board").status_code == 200
    assert seen["slugs"] == frozenset({"web"})


def test_apps_list_hides_unlisted_apps(monkeypatch, app_server):
    async def stats(s, *, now):
        return {}

    class S(Sessions):
        @asynccontextmanager
        async def _ctx(self):
            yield SimpleNamespace(scalars=_none)

    async def _none(q):
        return SimpleNamespace(all=lambda: [])

    monkeypatch.setattr(apps_routes, "app_stats", stats)
    object.__setattr__(app_server.state.deps, "sessions", S())
    c = _viewer_client(app_server)
    assert [a["slug"] for a in c.get("/api/apps").json()] == ["web"]
    c = _viewer_client(app_server, "ana@x.io", "approver")
    assert [a["slug"] for a in c.get("/api/apps").json()] == ["web", "payments"]


def test_create_job_needs_app_submitter(client_as):
    r = client_as("v@x.io", "submitter").post("/api/jobs", json={"app": "payments", "feature": "x"})
    assert r.status_code == 403


def test_events_limited_to_visible_apps(client_as, fake_bus):
    c = _viewer_client(_app_server(bus=fake_bus))
    c.get("/api/events")
    assert fake_bus.args is not None and fake_bus.args["app_slugs"] == frozenset({"web"})
    assert c.get("/api/events?app=payments").status_code == 404


def uses_job_access(route):
    def walk(dep):
        if hasattr(dep.call, "_job_access"):
            return True
        return any(walk(d) for d in dep.dependencies)

    return walk(route.dependant)


def _flat(routes):
    for r in routes:
        if hasattr(r, "original_router"):  # newer FastAPI nests included routers
            yield from _flat(r.original_router.routes)
        else:
            yield r


def test_every_job_route_checks_app_access(app_server):
    routes = list(_flat(app_server.routes))
    job_paths = [r for r in routes if "/jobs/{ref}" in r.path]
    assert len(job_paths) >= 6
    assert [r.path for r in job_paths if not uses_job_access(r)] == []


def test_hash_token_import_used():
    assert hash_token("x")
