import asyncio
from types import SimpleNamespace

import pytest
import yaml
from tests.unit.api import config_support as cs
from tests.unit.api.api_support import server

from swfactory.api.routes import definition as definition_routes
from swfactory.api.routes.profiles import unavailable_aliases
from swfactory.definition.models import Profile

OK_YAML = yaml.safe_dump(
    {
        "roles": {
            r: {"engine": "opencode", "model": "gemini-flash@vertex"}
            for r in (
                "spec-writer",
                "builder",
                "reviewer-security",
                "reviewer-ux",
                "reviewer-ui",
                "reviewer-code",
                "approver",
            )
        }
    }
)
YAML_WITH_DISABLED = OK_YAML.replace("gemini-flash@vertex", "claude-sonnet@vertex", 1)


def profile(**roles):
    base = {"builder": ("opencode", "gemini-pro@vertex")}
    base.update({k: v if isinstance(v, tuple) else ("opencode", v) for k, v in roles.items()})
    return Profile.model_validate(
        {"roles": {r.replace("_", "-"): {"engine": e, "model": m} for r, (e, m) in base.items()}}
    )


def put_profile(client, name, body, message="edit"):
    return client.put(f"/api/profiles/{name}", json={"yaml": body, "message": message})


@pytest.fixture
def fake_writer():
    return cs.FakeWriter()


@pytest.fixture
def fake_notify(monkeypatch):
    class Notify:
        channels: list[str] = []
        payloads: list[dict] = []

    n = Notify()
    n.channels, n.payloads = [], []

    async def notify(deps, payload):
        n.channels.append("definition_sync")
        n.payloads.append(payload)

    monkeypatch.setattr(definition_routes, "notify_sync", notify)

    async def position(deps):
        return 7

    monkeypatch.setattr(definition_routes, "stream_position_now", position)
    return n


@pytest.fixture
def fake_store():
    class Store:
        saves: list = []

    s = Store()
    s.saves = []
    return s


@pytest.fixture
def fake_events(monkeypatch, fake_writer):
    """Delivers definition.loaded {sha: fake_writer.sha}; `silent` makes it never arrive."""

    class Events:
        silent = False

        async def subscribe(self, *, after_id=None, **kw):
            assert after_id == 7
            if self.silent:
                await asyncio.sleep(3600)
            yield SimpleNamespace(kind="other", data={})
            yield SimpleNamespace(kind="definition.loaded", data={"sha": fake_writer.sha})

    return Events()


@pytest.fixture
def client_admin(fake_writer, fake_notify, fake_events):
    return cs.client_for(writer=fake_writer, events=fake_events)


def test_unavailable_alias_rejected():
    models = [cs.info("gemini-pro@vertex", available=False)]
    assert unavailable_aliases(profile(builder="gemini-pro@vertex"), models) == [
        "gemini-pro@vertex"
    ]


def test_missing_alias_is_unavailable():
    assert unavailable_aliases(profile(builder="nope@vertex"), cs.CATALOG) == ["nope@vertex"]


def test_experimental_needs_opt_in():
    p = profile(builder=("claude_code", "gemini-flash@vertex"))
    assert unavailable_aliases(p, cs.CATALOG) == ["gemini-flash@vertex"]
    opt_in = Profile.model_validate(
        {
            "roles": {
                "builder": {
                    "engine": "claude_code",
                    "model": "gemini-flash@vertex",
                    "allow_experimental": True,
                }
            }
        }
    )
    assert unavailable_aliases(opt_in, cs.CATALOG) == []


def test_engine_no_support_rejected():
    models = [cs.info("m@v", tools=False)]
    assert unavailable_aliases(profile(builder=("opencode", "m@v")), models) == ["m@v"]


def test_fallback_and_tiers_checked():
    p = Profile.model_validate(
        {
            "roles": {
                "builder": {
                    "engine": "opencode",
                    "model": "gemini-pro@vertex",
                    "fallback": ["x@v"],
                    "tiers": [{"label": "A", "model": "y@v", "description": "d"}],
                }
            }
        }
    )
    assert unavailable_aliases(p, cs.CATALOG) == ["x@v", "y@v"]


def test_put_profile_422_lists_disabled_alias(fake_writer, fake_notify):
    models = [m for m in cs.CATALOG if m.alias != "claude-sonnet@vertex"] + [
        cs.info("claude-sonnet@vertex", available=False)
    ]
    c = cs.client_for(writer=fake_writer, catalog=cs.FakeCatalog(models))
    r = put_profile(c, "team-gemini", YAML_WITH_DISABLED)
    assert r.status_code == 422 and r.json()["detail"]["unavailable"] == ["claude-sonnet@vertex"]
    assert fake_writer.paths == []


def test_put_profile_invalid_yaml_422(client_admin):
    assert put_profile(client_admin, "x", "builder: [").status_code == 422
    assert put_profile(client_admin, "x", "- a").status_code == 422


def test_put_profile_invalid_definition_422(client_admin, fake_writer):
    bad = OK_YAML.replace("reviewer-code:", "judge:")  # drops a required role
    r = put_profile(client_admin, "x", bad)
    assert r.status_code == 422 and r.json()["detail"]["errors"] and fake_writer.paths == []


@pytest.mark.parametrize("name", ["..%2Ffactory", "..", "Team", "a" * 41, "x.yaml"])
def test_profile_name_must_be_a_slug(client_admin, fake_writer, name):
    assert put_profile(client_admin, name, OK_YAML).status_code in (404, 422)
    assert fake_writer.paths == []
    assert client_admin.get(f"/api/profiles/{name}").status_code in (404, 422)


def test_put_profile_needs_admin(fake_writer):
    c = cs.client_for(mode="server", email="appr@x.io", role="approver", writer=fake_writer)
    assert put_profile(c, "team-gemini", OK_YAML).status_code == 403


def test_api_sync_only_notifies_in_personal_mode_too(client_admin, fake_notify, fake_store):
    assert client_admin.post("/api/definition/sync").status_code == 202
    assert fake_notify.channels == ["definition_sync"] and fake_store.saves == []


def test_profile_save_waits_for_the_worker_sync(
    client_admin, fake_writer, fake_notify, fake_events
):
    r = put_profile(client_admin, "team-gemini", OK_YAML)
    assert (r.status_code, r.json()["active_sha"], fake_notify.channels) == (
        200,
        fake_writer.sha,
        ["definition_sync"],
    )
    assert fake_writer.paths == ["profiles/team-gemini.yaml"]


def test_profile_save_times_out_with_202(fake_writer, fake_notify, fake_events, monkeypatch):
    from swfactory.api.routes import profiles

    monkeypatch.setattr(profiles, "LOADED_TIMEOUT_S", 0.05)
    fake_events.silent = True
    c = cs.client_for(writer=fake_writer, events=fake_events)
    r = put_profile(c, "team-gemini", OK_YAML)
    assert r.status_code == 202 and r.json()["active_sha"] is None


def test_put_profile_writer_conflict_409(fake_notify):
    c = cs.client_for(writer=cs.FakeWriter(fail=True))
    assert put_profile(c, "team-gemini", OK_YAML).status_code == 409


def test_server_without_writer_501():
    c = cs.client_for(mode="server")
    assert put_profile(c, "x", OK_YAML).status_code == 501


def test_put_profile_server_uses_settings_policy(fake_writer):
    defn = cs.default_definition()
    runners = {
        k: v.model_copy(update={"image": f"registry.example/team/runner@{cs.DIGEST}"})
        for k, v in defn.runners.items()
    }
    defn = defn.model_copy(update={"runners": runners})
    s = server(FACTORY_ALLOWED_IMAGE_PREFIXES="registry.example/team/")
    c = cs.client_for(mode="server", writer=fake_writer, defn=defn, s=s)
    r = put_profile(c, "team-gemini", OK_YAML)
    assert r.status_code == 200, r.text
    assert fake_writer.paths == ["profiles/team-gemini.yaml"]
    assert r.json()["active_sha"] is None and r.json()["mode"] == "commit"


def test_get_profiles_lists_and_reads(client_admin):
    names = [p["name"] for p in client_admin.get("/api/profiles").json()]
    assert "team-gemini" in names
    one = client_admin.get("/api/profiles/team-gemini").json()
    assert one["name"] == "team-gemini" and "builder" in one["roles"]
    assert yaml.safe_load(one["yaml"])["roles"]["builder"]["model"] == "gemini-pro@vertex"
    assert client_admin.get("/api/profiles/nope").status_code == 404
