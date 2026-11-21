import dataclasses
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.unit.api.api_support import FakeDefinitions, FakeKeys, fake_deps, server

from swfactory.api.app import create_app
from swfactory.api.auth import hash_token
from swfactory.api.routes import improve as improve_routes
from swfactory.domain.types import Role

ROLES = {"viewer@x.io": "viewer", "approver@x.io": "approver", "admin@x.io": "admin"}
IID = str(uuid4())
ROW = {
    "id": IID, "role_group": "builder", "trigger": "manual", "status": "branch_ready",
    "branch": "improve/20261111-builder", "pr_url": None, "base_sha": "a" * 40,
    "proposed_sha": "b" * 40, "result": "better", "hidden_before": 0.5, "hidden_after": 0.75,
    "cost_usd": "48.600000", "created_at": "2026-11-11T00:00:00+00:00",
}  # fmt: skip


class Users:
    async def role_of(self, email):
        return ROLES.get(email)

    async def token_email(self, token_hash):
        return next((e for e in ROLES if hash_token("sfp_" + e) == token_hash), None)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def begin(self):
        return self


@pytest.fixture
def seen(monkeypatch):
    calls = []

    async def lst(s, limit):
        calls.append(("list", limit))
        return [ROW]

    async def one(s, artifacts, iid):
        if str(iid) != IID:
            return None
        return {**ROW, "evidence": {"signals": [], "changes": [], "comparison": [],
                                    "violations": [], "costs": {}}}  # fmt: skip

    async def cycle(s, defn, *, trigger, role=None, now=None):
        calls.append(("cycle", trigger, role))
        if role == Role.JUDGE:
            raise ValueError("set improver.bench_suite first")
        return [uuid4()]

    monkeypatch.setattr(improve_routes, "load_improvements", lst)
    monkeypatch.setattr(improve_routes, "load_improvement", one)
    monkeypatch.setattr(improve_routes, "start_cycle", cycle)
    return calls


@pytest.fixture
def client_as(seen):
    s = server()
    deps = dataclasses.replace(
        fake_deps(s),
        sessions=lambda: _Tx(),  # pyright: ignore[reportArgumentType]
        definitions=FakeDefinitions(),  # pyright: ignore[reportArgumentType]
        users=Users(),  # pyright: ignore[reportArgumentType]
    )
    app = create_app(s, deps, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]

    def make(role):
        return TestClient(
            app,
            base_url="http://127.0.0.1:7080",
            headers={"authorization": f"Bearer sfp_{role}@x.io"},
        )

    return make


def test_list_shape(client_as):
    r = client_as("viewer").get("/api/improvements").json()[0]
    assert (r["cost_usd"], r["result"]) == ("48.600000", "better")


def test_list_limit_is_validated(client_as, seen):
    assert client_as("viewer").get("/api/improvements?limit=0").status_code == 422
    assert client_as("viewer").get("/api/improvements?limit=7").status_code == 200
    assert seen[-1] == ("list", 7)


def test_detail_has_evidence(client_as):
    r = client_as("viewer").get(f"/api/improvements/{IID}").json()
    assert set(r["evidence"]) == {"signals", "changes", "comparison", "violations", "costs"}
    assert client_as("viewer").get(f"/api/improvements/{uuid4()}").status_code == 404
    assert client_as("viewer").get("/api/improvements/nope").status_code == 404


def test_run_needs_admin(client_as):
    assert client_as("approver").post("/api/improvements/run", json={}).status_code == 403


def test_run_manual(client_as, seen):
    r = client_as("admin").post("/api/improvements/run", json={"role": "builder"})
    assert len(r.json()["ids"]) == 1
    assert seen[-1] == ("cycle", "manual", Role.BUILDER)


def test_run_errors(client_as):
    c = client_as("admin")
    assert c.post("/api/improvements/run", json={"role": "wizard"}).status_code == 422
    assert c.post("/api/improvements/run", json={"role": "judge"}).status_code == 422
    assert c.post("/api/improvements/run", json={"x": 1}).status_code == 422


def test_improver_api_has_no_merge_route():
    paths = [getattr(r, "path", "") for r in improve_routes.router.routes]
    assert paths and not [p for p in paths if "merge" in p or "approve" in p]
