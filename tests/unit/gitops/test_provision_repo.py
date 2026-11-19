import json
from typing import Any

import httpx
import pytest
import respx

from swfactory.gitops.github import ProvisionedRepo, provision_repo

API = "https://api.github.com"
JWT = {"Authorization": "Bearer jwt"}


class Auth:
    """App-style auth: the org token narrows to the org, the JWT proves installation."""

    async def token_for(self, repo: str, permissions: Any) -> str:
        return "tok"

    async def org_token(self, org: str, permissions: Any) -> str:
        self.last_perms = dict(permissions)
        return "orgtok"

    def app_headers(self) -> dict[str, str]:
        return dict(JWT)


class FakeGh:
    def __init__(self, m: respx.MockRouter) -> None:
        self.m = m
        self.installed = True
        self.existing = False
        self.owner_type = "Organization"
        self.log: list[tuple[str, dict[str, Any] | None]] = []
        m.route().mock(side_effect=self._handle)

    @property
    def calls(self) -> list[str]:
        return [c for c, _ in self.log]

    def last_body(self, call: str) -> Any:
        return [b for c, b in self.log if c == call][-1]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        call = f"{request.method} {request.url.path}"
        body: Any = json.loads(request.content) if request.content else None
        self.log.append((call, body))
        path = request.url.path
        if call.endswith("/installation"):
            return httpx.Response(200 if self.installed else 404, json={"id": 1})
        if request.method == "GET" and path.startswith("/users/"):
            return httpx.Response(200, json={"type": self.owner_type})
        if request.method == "GET" and path.startswith("/repos/"):
            if self.existing:
                return httpx.Response(200, json=self._repo(path.removeprefix("/repos/")))
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "POST":
            name = body["name"]
            if path == "/user/repos":
                owner = "me"
            elif path.startswith("/orgs/"):
                owner = path.split("/")[2]
            else:
                owner = body["owner"]
            return httpx.Response(201, json=self._repo(f"{owner}/{name}"))
        if request.method == "PATCH":
            return httpx.Response(200, json=self._repo(path.removeprefix("/repos/")))
        return httpx.Response(500)

    @staticmethod
    def _repo(full: str) -> dict[str, Any]:
        return {
            "full_name": full,
            "html_url": f"https://github.com/{full}",
            "default_branch": "main",
        }


@pytest.fixture
def fake_gh():
    with respx.mock(base_url=API, assert_all_called=False) as m:
        yield FakeGh(m)


async def test_org_repo_has_auto_merge(fake_gh):
    auth = Auth()
    r = await provision_repo(auth, full_name="acme/demo", template=None, private=True)
    assert fake_gh.last_body("POST /orgs/acme/repos") == {
        "name": "demo",
        "private": True,
        "auto_init": True,
        "allow_auto_merge": True,
        "delete_branch_on_merge": True,
    }
    assert auth.last_perms == {"administration": "write", "contents": "write", "metadata": "read"}
    assert r == ProvisionedRepo("acme/demo", "https://github.com/acme/demo", "main", True)


async def test_public_flag(fake_gh):
    await provision_repo(Auth(), full_name="acme/demo", template=None, private=False)
    assert fake_gh.last_body("POST /orgs/acme/repos")["private"] is False


async def test_from_template(fake_gh):
    await provision_repo(Auth(), full_name="acme/demo", template="acme/py-template", private=True)
    assert fake_gh.last_body("POST /repos/acme/py-template/generate") == {
        "owner": "acme",
        "name": "demo",
        "private": True,
        "include_all_branches": False,
    }
    assert fake_gh.last_body("PATCH /repos/acme/demo") == {
        "allow_auto_merge": True,
        "delete_branch_on_merge": True,
    }


async def test_existing_repo_is_not_recreated(fake_gh):
    fake_gh.existing = True
    r = await provision_repo(Auth(), full_name="acme/demo", template=None, private=True)
    assert "POST /orgs/acme/repos" not in fake_gh.calls
    assert not any(c.startswith("PATCH") for c in fake_gh.calls)
    assert r.full_name == "acme/demo"


async def test_unreachable_is_reported(fake_gh):
    fake_gh.installed = False
    r = await provision_repo(Auth(), full_name="acme/demo", template=None, private=True)
    assert r.reachable is False


class PatOnly:
    async def token_for(self, repo: str, permissions: Any) -> str:
        return "pat"

    async def org_token(self, org: str, permissions: Any) -> str:
        return "pat"


async def test_pat_user_repo_uses_user_endpoint(fake_gh):
    fake_gh.owner_type = "User"
    r = await provision_repo(PatOnly(), full_name="me/demo", template=None, private=True)
    assert fake_gh.last_body("POST /user/repos")["name"] == "demo"
    assert "POST /orgs/me/repos" not in fake_gh.calls
    assert r.reachable is True  # a PAT reaches every repo it can create
