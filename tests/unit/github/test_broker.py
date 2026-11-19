import httpx
import pytest
import respx

from swfactory.github.auth import (
    PERMS_COMMENT,
    PERMS_DEFINITION_PR,
    PERMS_DELIVER,
    PERMS_PACKAGES_READ,
    PERMS_PUSH,
)
from swfactory.github.broker import BrokerAuth, TokenBroker, broker_app

REQ = {"repo": "acme/def", "permissions": PERMS_DEFINITION_PR}


class FakeAppAuth:
    def __init__(self):
        self.minted = []
        self.org_calls = []

    async def token_for(self, repo, permissions):
        self.minted.append((repo, dict(permissions)))
        return f"tok-{repo}"

    async def org_token(self, org, permissions):
        self.org_calls.append((org, dict(permissions)))
        return f"org-{org}"


async def _repos():
    return {"acme/web"}


@pytest.fixture
def fake_auth():
    return FakeAppAuth()


@pytest.fixture
def broker(fake_auth):
    return TokenBroker(fake_auth, definition_repo="acme/def", app_repos=_repos)


@pytest.fixture
async def broker_client(broker):
    transport = httpx.ASGITransport(app=broker_app(broker, shared_token="s3cret"))
    async with httpx.AsyncClient(transport=transport, base_url="http://broker") as c:
        yield c


async def test_broker_issues_only_narrow_tokens(fake_auth):
    b = TokenBroker(fake_auth, definition_repo="acme/def", app_repos=_repos)
    assert await b.issue("acme/def", PERMS_DEFINITION_PR)
    assert await b.issue("acme/web", PERMS_COMMENT)
    for repo, perms in [
        ("acme/web", PERMS_DELIVER),
        ("acme/web", PERMS_PUSH),
        ("acme/other", PERMS_COMMENT),
        ("acme/def", PERMS_DELIVER),
        ("acme/def", PERMS_COMMENT),
        ("acme/web", PERMS_DEFINITION_PR),
    ]:
        with pytest.raises(PermissionError):
            await b.issue(repo, perms)
    assert len(fake_auth.minted) == 2


async def test_broker_without_definition_repo_refuses_it(fake_auth):
    b = TokenBroker(fake_auth, definition_repo=None, app_repos=_repos)
    with pytest.raises(PermissionError):
        await b.issue("acme/def", PERMS_DEFINITION_PR)


async def test_broker_http_needs_shared_token(broker_client):
    path = "/internal/github-token"
    assert (await broker_client.post(path, json=REQ)).status_code == 401
    bad = {"authorization": "Bearer nope"}
    assert (await broker_client.post(path, json=REQ, headers=bad)).status_code == 401
    ok = await broker_client.post(path, json=REQ, headers={"authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and ok.json() == {"token": "tok-acme/def"}


async def test_broker_http_refuses_wide_requests(broker_client):
    h = {"authorization": "Bearer s3cret"}
    wide = {"repo": "acme/web", "permissions": PERMS_PUSH}
    assert (
        await broker_client.post("/internal/github-token", json=wide, headers=h)
    ).status_code == 403
    r = await broker_client.post("/internal/github-token", json={"repo": 1}, headers=h)
    assert r.status_code == 400


@respx.mock
async def test_broker_auth_posts_and_caches(tmp_path):
    f = tmp_path / "t"
    f.write_text("s3cret\n")
    now = [0.0]
    route = respx.post("http://orchestrator:8091/internal/github-token").mock(
        return_value=httpx.Response(200, json={"token": "ghs_abc"})
    )
    a = BrokerAuth("http://orchestrator:8091", f, clock=lambda: now[0])
    assert await a.token_for("acme/def", PERMS_DEFINITION_PR) == "ghs_abc"
    assert await a.token_for("acme/def", PERMS_DEFINITION_PR) == "ghs_abc"
    assert route.call_count == 1
    assert route.calls[0].request.headers["authorization"] == "Bearer s3cret"
    now[0] = 51 * 60
    await a.token_for("acme/def", PERMS_DEFINITION_PR)
    assert route.call_count == 2
    with pytest.raises(PermissionError):
        await a.org_token("acme", PERMS_COMMENT)


async def test_broker_issues_packages_read_only_to_an_owning_org(fake_auth):
    b = TokenBroker(fake_auth, definition_repo="acme/def", app_repos=_repos)
    assert await b.issue_packages_read("acme")
    assert fake_auth.org_calls == [("acme", PERMS_PACKAGES_READ)]
    with pytest.raises(PermissionError):
        await b.issue_packages_read("other")


async def test_packages_route_needs_the_shared_token(broker_client):
    body = {"org": "acme"}
    path = "/internal/github-packages-token"
    assert (await broker_client.post(path, json=body)).status_code == 401
    h = {"authorization": "Bearer s3cret"}
    ok = await broker_client.post(path, json=body, headers=h)
    assert ok.status_code == 200 and set(ok.json()) == {"token"}
    no = await broker_client.post(path, json={"org": "other"}, headers=h)
    assert no.status_code == 403
