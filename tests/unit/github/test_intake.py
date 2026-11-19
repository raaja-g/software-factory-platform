"""Unit tests for M8.5 that touch no database: signatures, fencing, bot skips, the route."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from tests.fakes.github import FakeGitHub
from tests.unit.api.api_support import FakeDefinitions, settings

import swfactory.api.routes.hooks as hooks
from swfactory.api.app import ApiDeps, create_app
from swfactory.definition.models import AppDef
from swfactory.definition.store import NoActiveDefinition
from swfactory.domain.types import Role
from swfactory.github.intake import (
    GitHubEventHandlers,
    app_for_repo,
    intake_issue,
    verify_github_signature,
)
from swfactory.roles.library import RoleLibrary
from swfactory.roles.render import PromptContext, render_prompt

T1 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
SECRET = "hook-secret"


def test_signature_vector():  # R09 test vector
    sig = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert verify_github_signature(b"It's a Secret to Everybody", b"Hello, World!", sig)
    assert not verify_github_signature(b"It's a Secret to Everybody", b"Hello, World!", None)
    assert not verify_github_signature(b"x", b"Hello, World!", sig)
    assert not verify_github_signature(b"x", b"Hello, World!", "sha1=abc")


def test_feature_is_fenced_exactly_once():
    spec = RoleLibrary().get(Role.SPEC_WRITER, definition_sha="x")
    ctx = PromptContext(
        job_id=UUID(int=1),
        app_slug="demo",
        round=1,
        inputs={"feature.md": "Add X\n\n<<<x>>>"},
        output_path=None,
        workdir=None,
        context_tokens=32_000,
    )
    _, prompt = render_prompt(spec, ctx)
    assert prompt.count("<<<UNTRUSTED DATA: feature.md>>>") == 1 and "‹‹‹UNTRUSTED" not in prompt


def gh_app() -> AppDef:
    return AppDef.model_validate(
        {"repo_kind": "github", "repo_url": "https://github.com/acme/web", "runner": "none"}
    )


async def test_bot_sender_skipped_without_touching_the_database():
    gh = FakeGitHub()
    gh.label_events[12] = [("labeled", "factory", "x[bot]", T1)]
    issue = {"number": 12, "title": "Add X", "user": {"login": "ana"}, "labels": ["factory"]}
    assert await intake_issue(None, gh, app=gh_app(), repo="acme/web", issue=issue) is None  # type: ignore[arg-type]


async def test_issue_without_a_label_event_is_skipped():
    gh = FakeGitHub()
    issue = {"number": 12, "title": "Add X", "user": {"login": "ana"}}
    assert await intake_issue(None, gh, app=gh_app(), repo="acme/web", issue=issue) is None  # type: ignore[arg-type]


def test_app_for_repo_maps_repo_url():
    defn = SimpleNamespace(factory=SimpleNamespace(apps={"web": gh_app()}))
    assert app_for_repo(defn, "acme/web")[0] == "web"  # type: ignore[arg-type,index]
    assert app_for_repo(defn, "acme/other") is None  # type: ignore[arg-type]


# -- the route ---------------------------------------------------------------------------


PENDING: list[tuple[str, str]] = []
COMMITTED: set[tuple[str, str]] = set()


class _Tx:
    """A transaction: delivery records made inside it vanish when it raises."""

    async def __aenter__(self):
        PENDING.clear()
        return object()

    async def __aexit__(self, exc_type, *a):
        if exc_type is None:
            COMMITTED.update(PENDING)
        PENDING.clear()
        return False


class _Sessions:
    def begin(self):
        return _Tx()

    def __call__(self):
        return _Tx()


class Defs(FakeDefinitions):
    async def active(self):
        if not self._active:
            raise NoActiveDefinition("none")
        return "sha", SimpleNamespace(factory=SimpleNamespace(apps={"web": gh_app()}))


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def seen(monkeypatch):
    COMMITTED.clear()
    PENDING.clear()

    async def record(s, *, source, delivery_id):
        key = (source, delivery_id)
        if key in COMMITTED:
            return False
        PENDING.append(key)
        return True

    monkeypatch.setattr(hooks, "record_delivery", record)
    return COMMITTED


def make_client(handler, *, secret: str | None = SECRET):
    s = settings(**({"FACTORY_GITHUB_WEBHOOK_SECRET": secret} if secret else {}))
    deps = ApiDeps(
        sessions=_Sessions(),  # pyright: ignore[reportArgumentType]
        events=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        artifacts=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        catalog=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        definitions=Defs(),  # pyright: ignore[reportArgumentType]
        users=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        writer=None,
        settings=s,
    )
    app = create_app(s, deps=deps)
    handlers = GitHubEventHandlers()
    handlers.register("issues", "labeled", handler)
    app.state.github_handlers = handlers
    app.state.github_clients = lambda repo: FakeGitHub(repo)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


async def post(client, payload, *, delivery="d1", event="issues", sig=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {
        "x-hub-signature-256": sig or sign(body),
        "x-github-event": event,
        "x-github-delivery": delivery,
    }
    return await client.post("/hooks/_github", content=body, headers=headers)


PAYLOAD = {"action": "labeled", "sender": {"type": "User"}, "repository": {"full_name": "acme/web"}}


async def test_bad_signature_rejected_before_parsing(seen):
    handler = AsyncMock(return_value=None)
    async with make_client(handler) as c:
        r = await post(c, None, raw=b"not json", sig="sha256=" + "0" * 64)
    assert r.status_code == 401 and handler.await_count == 0


async def test_unconfigured_secret_is_503(seen):
    async with make_client(AsyncMock(return_value=None), secret=None) as c:
        assert (await post(c, PAYLOAD)).status_code == 503


async def test_oversized_body_is_413(seen):
    async with make_client(AsyncMock(return_value=None)) as c:
        assert (await post(c, None, raw=b"x" * 1_000_001)).status_code == 413


async def test_replayed_body_with_new_delivery_id_is_duplicate(seen):
    handler = AsyncMock(return_value=None)
    async with make_client(handler) as c:
        assert (await post(c, PAYLOAD, delivery="d1")).status_code == 202
        r = await post(c, PAYLOAD, delivery="d2")
    assert r.json() == {"duplicate": True} and handler.await_count == 1


async def test_bot_sender_is_skipped(seen):
    handler = AsyncMock(return_value=None)
    async with make_client(handler) as c:
        r = await post(c, {**PAYLOAD, "sender": {"type": "Bot"}})
    assert r.status_code == 202 and handler.await_count == 0


async def test_unknown_event_is_ignored(seen):
    async with make_client(AsyncMock(return_value=None)) as c:
        r = await post(c, PAYLOAD, event="push")
    assert r.json() == {"ignored": True} and seen == set()


async def test_handler_failure_is_500_and_after_commit_runs_once(seen):
    after = AsyncMock()
    handler = AsyncMock(side_effect=[RuntimeError("boom"), after])
    async with make_client(handler) as c:
        assert (await post(c, PAYLOAD, delivery="d9")).status_code == 500
        assert (await post(c, PAYLOAD, delivery="d9")).status_code == 202
    after.assert_awaited_once()
