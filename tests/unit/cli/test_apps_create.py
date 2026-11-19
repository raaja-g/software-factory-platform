import httpx
import pytest
import respx
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.settings import get_settings

API = "https://api.github.com"


class Auth:
    async def token_for(self, repo, permissions):
        return "tok"

    async def org_token(self, org, permissions):
        return "orgtok"

    def app_headers(self):
        return {"Authorization": "Bearer jwt"}


class Gh:
    installed = True


@pytest.fixture
def fake_gh(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env
    get_settings.cache_clear()
    monkeypatch.setattr("swfactory.github.auth.make_auth", lambda settings, http=None: Auth())
    gh = Gh()

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/installation"):
            return httpx.Response(200 if gh.installed else 404, json={"id": 1})
        if request.method == "GET":
            return httpx.Response(404, json={})
        return httpx.Response(
            201,
            json={
                "full_name": "acme/demo",
                "html_url": "https://github.com/acme/demo",
                "default_branch": "main",
            },
        )

    with respx.mock(base_url=API, assert_all_called=False) as m:
        m.route().mock(side_effect=handle)
        yield gh
    get_settings.cache_clear()


def cli(*args: str):
    return CliRunner().invoke(app, list(args))


def test_cli_prints_snippet_and_fails_when_unreachable(fake_gh):
    out = cli("apps", "create", "demo", "--github", "acme/demo")
    assert out.exit_code == 0, out.stdout
    for line in (
        "merge_policy: pr_auto_merge",
        "installation: reachable",
        "ci_safe: true",
        "repo_kind: github",
        "repo_url: https://github.com/acme/demo",
        "default_branch: main",
        "Set ci_safe to false if organization secrets are available to this repo.",
    ):
        assert line in out.stdout
    fake_gh.installed = False
    r = cli("apps", "create", "demo", "--github", "acme/demo")
    assert r.exit_code == 2
    assert "add acme/demo to the app installation" in r.stdout
    assert "merge_policy: pr_auto_merge" in r.stdout


def test_bad_github_value(fake_gh):
    assert cli("apps", "create", "demo", "--github", "nope").exit_code == 2
