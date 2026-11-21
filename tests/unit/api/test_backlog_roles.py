import dataclasses
from types import SimpleNamespace

import httpx
import pytest
from tests.unit.api.api_support import FakeDefinitions, FakeKeys, fake_deps, server

from swfactory.api.app import create_app
from swfactory.api.auth import hash_token
from swfactory.api.routes import backlogs as backlogs_routes

IMPORT_BODY = {"app": "todo", "title": "v1", "items": [{"id": "1", "title": "a", "feature": "f"}]}
ROLES = {
    "viewer@x.io": "viewer",
    "sub@x.io": "submitter",
    "ap@x.io": "approver",
    "ad@x.io": "admin",
}


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
    def __init__(self, access):
        super().__init__()
        app = SimpleNamespace(access=access)
        defaults = SimpleNamespace(max_backlog_items=5)
        self.defn = SimpleNamespace(factory=SimpleNamespace(apps={"todo": app}, defaults=defaults))

    async def active(self):
        return "sha", self.defn


def make_client(monkeypatch, access=None):
    async def find(s, ref):
        return SimpleNamespace(id="bid"), "todo"

    async def apply(s, *, backlog_id, action, by):
        return "cancelled"

    monkeypatch.setattr(backlogs_routes, "_find", find)
    monkeypatch.setattr(backlogs_routes, "apply_backlog_action", apply)
    deps = dataclasses.replace(
        fake_deps(),
        sessions=_Sessions(),  # pyright: ignore[reportArgumentType]
        definitions=Defs(access),  # pyright: ignore[reportArgumentType]
        users=Users(),  # pyright: ignore[reportArgumentType]
    )
    app = create_app(server(), deps, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]

    def client(role):
        email = next(e for e, r in ROLES.items() if r == role)
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7080"
        )

        async def post(url, body):
            r = await c.post(url, json=body, headers={"authorization": f"Bearer sfp_{email}"})
            return r.status_code, r.json()

        async def get(url):
            r = await c.get(url, headers={"authorization": f"Bearer sfp_{email}"})
            return r.status_code, r.json()

        return SimpleNamespace(post=post, get=get)

    return client


@pytest.fixture
def api_client(monkeypatch):
    return make_client(monkeypatch)


@pytest.fixture
def api_client_no_access(monkeypatch):
    return make_client(
        monkeypatch, SimpleNamespace(viewers=("other@x.io",), submitters=(), approvers=())
    )


@pytest.mark.parametrize(
    "role,status", [("viewer", 403), ("submitter", 403), ("approver", 200), ("admin", 200)]
)
async def test_backlog_actions_need_the_approver_role(api_client, role, status):
    assert (await api_client(role).post("/api/backlogs/todo:b001/actions", {"action": "cancel"}))[
        0
    ] == status


async def test_import_needs_the_submitter_role(api_client):
    assert (await api_client("viewer").post("/api/backlogs/import", IMPORT_BODY))[0] == 403


async def test_a_backlog_of_a_hidden_app_is_404(api_client_no_access):
    assert (await api_client_no_access("viewer").get("/api/backlogs/todo:b001"))[0] == 404


async def test_an_unlisted_approver_cannot_act_on_a_hidden_app(api_client_no_access):
    c = api_client_no_access("approver")
    assert (await c.post("/api/backlogs/todo:b001/actions", {"action": "cancel"}))[0] == 404
