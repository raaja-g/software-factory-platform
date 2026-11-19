from decimal import Decimal

import pytest
from tests.unit.api import config_support as cs

from swfactory.api.routes import apps as apps_routes
from swfactory.api.routes import definition as definition_routes
from swfactory.api.routes import models as models_routes


class FakeEvents:
    def __init__(self):
        self.records: list[tuple] = []
        self.ids: list[int] = []
        self.refreshed: dict[int, dict] = {7: {"request_id": 7, "aliases": None, "available": 2}}

    async def record(self, s, job_id, kind, data):
        self.records.append((job_id, kind, data))
        self.ids.append(41 + len(self.ids))
        return self.ids[-1]


@pytest.fixture
def fake_events(monkeypatch):
    ev = FakeEvents()
    monkeypatch.setattr(models_routes, "record_event", ev.record)

    async def refreshed(deps, request_id):
        return ev.refreshed.get(request_id)

    monkeypatch.setattr(models_routes, "refreshed_event", refreshed)
    return ev


@pytest.fixture
def fake_catalog():
    return cs.FakeCatalog()


@pytest.fixture
def client_admin(fake_catalog):
    return cs.client_for(catalog=fake_catalog)


def role_client(role):
    return cs.client_for(mode="server", email=f"{role}@x.io", role=role)


def test_models_refresh_needs_admin():
    assert role_client("approver").post("/api/models/refresh").status_code == 403


def test_models_refresh_records_a_request(client_admin, fake_events, fake_catalog):
    r = client_admin.post("/api/models/refresh", json={"aliases": ["flash@vertex"]})
    assert (r.status_code, r.json()) == (202, {"request_id": fake_events.ids[-1]})
    assert fake_catalog.refresh_calls == []
    assert fake_events.records[-1] == (
        None,
        "models.refresh_requested",
        {"aliases": ["flash@vertex"], "by": "local"},
    )


def test_models_refresh_without_body_means_all(client_admin, fake_events):
    assert client_admin.post("/api/models/refresh").status_code == 202
    assert fake_events.records[-1][2]["aliases"] is None


def test_models_refresh_status(client_admin, fake_events):
    assert client_admin.get("/api/models/refresh/8").json() == {"done": False}
    body = client_admin.get("/api/models/refresh/7").json()
    assert body["done"] is True and len(body["models"]) == len(cs.CATALOG)
    assert body["models"][0]["price_in_per_mtok"] == "1.5"


def test_models_refresh_status_needs_admin(fake_events):
    assert role_client("viewer").get("/api/models/refresh/7").status_code == 403


def test_models_list_filters_availability():
    cat = cs.FakeCatalog([cs.info("a@v"), cs.info("b@v", available=False)])
    c = cs.client_for(catalog=cat)
    assert [m["alias"] for m in c.get("/api/models").json()] == ["a@v", "b@v"]
    assert [m["alias"] for m in c.get("/api/models?available=true").json()] == ["a@v"]
    assert [m["alias"] for m in c.get("/api/models?available=false").json()] == ["b@v"]


def test_definition_status_shape(monkeypatch):
    async def fake_status(deps):
        return {"active_sha": "s", "source": "x", "loaded_at": None, "last_rejected": None}

    monkeypatch.setattr(definition_routes, "definition_status", fake_status)
    c = role_client("viewer")
    assert c.get("/api/definition").json().keys() == {
        "active_sha",
        "source",
        "loaded_at",
        "last_rejected",
    }


def test_definition_sync_needs_admin():
    assert role_client("approver").post("/api/definition/sync").status_code == 403


def test_definition_sync(monkeypatch):
    async def ok(deps):
        return "abc", {"demo": "added"}

    monkeypatch.setattr(definition_routes, "sync_definition", ok)
    r = cs.client_for().post("/api/definition/sync")
    assert r.json() == {"active_sha": "abc", "apps": {"demo": "added"}}

    async def bad(deps):
        raise definition_routes.SyncError(["no way"])

    monkeypatch.setattr(definition_routes, "sync_definition", bad)
    r = cs.client_for().post("/api/definition/sync")
    assert r.status_code == 422 and r.json()["detail"] == {"errors": ["no way"]}


def test_metrics_placeholder():
    assert role_client("viewer").get("/api/metrics").json() == {
        "available": False,
        "message": "Metrics arrive with scoring (M10).",
    }


def test_apps_join_definition_and_stats(monkeypatch):
    async def stats(s, *, now):
        counts = dict.fromkeys(apps_routes.COLUMNS, 0) | {"build": 2}
        return {"demo": (counts, Decimal("1.25"))}

    monkeypatch.setattr(apps_routes, "app_stats", stats)

    class Sess(cs._Sess):
        async def scalars(self, q):
            class R:
                def all(self):
                    return []

            return R()

    c = cs.client_for(sessions=lambda: Sess())
    [app] = c.get("/api/apps").json()
    assert app["slug"] == "demo" and app["repo_kind"] == "local"
    assert app["profile"] == "team-gemini" and app["merge_policy"] == "local_merge"
    assert app["counts"]["build"] == 2 and app["cost_today_usd"] == "1.25"
    assert isinstance(app["budget_usd"], str) and app["max_rounds"] == 2


def test_stage_columns_cover_every_stage():
    from swfactory.domain.types import Stage

    assert {apps_routes.stage_column(s.value) for s in Stage} == set(apps_routes.COLUMNS)
