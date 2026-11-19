import pytest
from tests.unit.api import config_support as cs
from tests.unit.api.api_support import server
from tests.unit.api.test_profiles import OK_YAML, put_profile

from swfactory.api import app as app_mod
from swfactory.github import broker
from swfactory.github.auth import AppAuth


class FakeGitHub:
    pr_url = "https://github.com/acme/def/pull/1"

    def __init__(self):
        self.calls: list[str] = []

    async def default_branch(self, repo):
        return "main"

    async def get_branch_sha(self, repo, branch):
        return "base"

    async def get_file_sha(self, repo, path, ref):
        return None

    async def create_ref(self, repo, ref, sha):
        self.calls.append("create_ref")

    async def create_or_update_file_contents(self, repo, path, **kw):
        self.calls.append("create_or_update_file_contents")

    async def create_pull(self, repo, **kw):
        self.calls.append("create_pull")
        return self.pr_url


@pytest.fixture
def fake_github(monkeypatch):
    gh = FakeGitHub()
    seen = []

    async def github_for(auth, repo, permissions, **kw):
        seen.append((type(auth).__name__, repo, dict(permissions)))
        return gh

    monkeypatch.setattr(broker, "github_for", github_for)
    real = broker.ServerDefinitionWriter
    monkeypatch.setattr(
        app_mod, "ServerDefinitionWriter", lambda auth, repo: real(auth, repo, min_interval=0)
    )
    gh.seen = seen  # type: ignore[attr-defined]
    return gh


def server_defn():
    defn = cs.default_definition()
    runners = {
        k: v.model_copy(update={"image": f"registry.example/team/runner@{cs.DIGEST}"})
        for k, v in defn.runners.items()
    }
    return defn.model_copy(update={"runners": runners})


def server_settings(tmp_path, **kw):
    f = tmp_path / "broker_token"
    f.write_text("s3cret")
    kw.setdefault("FACTORY_ALLOWED_IMAGE_PREFIXES", "registry.example/team/")
    kw.setdefault("FACTORY_DEFINITION", "https://github.com/acme/def")
    return server(FACTORY_BROKER_TOKEN_FILE=str(f), **kw)


def test_server_profile_save_opens_pr(tmp_path, fake_github):
    c = cs.client_for(mode="server", defn=server_defn(), s=server_settings(tmp_path))
    r = put_profile(c, "x", OK_YAML)
    assert r.status_code == 200 and r.json()["mode"] == "pr", r.text
    assert fake_github.calls == ["create_ref", "create_or_update_file_contents", "create_pull"]
    assert fake_github.seen == [("BrokerAuth", "acme/def", broker.PERMS_DEFINITION_PR)]


def test_server_without_github_definition_has_no_writer(tmp_path, fake_github):
    s = server_settings(tmp_path, FACTORY_DEFINITION="/srv/def")
    c = cs.client_for(mode="server", defn=server_defn(), s=s)
    assert put_profile(c, "x", OK_YAML).status_code == 501


def test_server_without_broker_token_has_no_writer(fake_github):
    s = server(FACTORY_DEFINITION="https://github.com/acme/def")
    c = cs.client_for(mode="server", defn=server_defn(), s=s)
    assert put_profile(c, "x", OK_YAML).status_code == 501


def test_api_never_builds_app_auth(tmp_path, fake_github):
    before = AppAuth.instances
    cs.client_for(mode="server", s=server_settings(tmp_path))
    assert AppAuth.instances == before
