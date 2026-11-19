import asyncio
import logging
from types import SimpleNamespace

import pytest
from tests.fakes.github import FakeGitHub

from swfactory.definition.loader import (
    GIT_FETCH,
    DefinitionError,
    LoadResult,
    defaults_dir,
    load_definition,
)
from swfactory.definition.store import NoActiveDefinition
from swfactory.definition.sync import (
    DEFINITION_CHECK,
    UNPROTECTED_DEFINITION,
    DefinitionSync,
    definition_hook_payload,
    definition_protected,
    definition_repo_slug,
)
from swfactory.domain.types import FactoryMode
from swfactory.github.auth import PERMS_FETCH, PatAuth
from swfactory.settings import Settings

SRC = "https://github.com/acme/factory-definition"
VALID = load_definition(str(defaults_dir())).definition
ERR = DefinitionError("factory.yaml", 3, "apps.demo", "bad")
PR_RULE = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]


def settings(source=SRC, mode="server", **kw):
    env = {
        "FACTORY_MODE": mode,
        "FACTORY_DEFINITION": source,
        "FACTORY_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:1/x",
        **kw,
    }
    return Settings(_env_file=None, **env)  # pyright: ignore[reportCallIssue]


class FakeLoader:
    def __init__(self):
        self.next: LoadResult | None = None
        self.calls: list[tuple[str, dict]] = []
        self.fetch_seen: list = []

    def __call__(self, source, **kw):
        self.calls.append((source, kw))
        self.fetch_seen.append(GIT_FETCH.get())
        assert self.next is not None
        return self.next


class FakeStore:
    def __init__(self, events):
        self.events = events
        self.active_sha: str | None = "a" * 40
        self.rows: dict[str, SimpleNamespace] = {}

    async def save(self, result, *, activate=True):
        self.rows[result.sha] = SimpleNamespace(errors=tuple(str(e) for e in result.errors))
        if result.definition is None or result.errors or not activate:
            return False
        self.active_sha = result.sha
        self.events.append("definition.loaded")
        return True

    async def active(self):
        if self.active_sha is None:
            raise NoActiveDefinition("none")
        return self.active_sha, VALID


class FakeAuth:
    def __init__(self):
        self.asked: list = []

    async def token_for(self, repo, permissions):
        self.asked.append((repo, dict(permissions)))
        return "tok"


@pytest.fixture
def events():
    return []


@pytest.fixture
def store(events):
    return FakeStore(events)


@pytest.fixture
def fake_loader():
    return FakeLoader()


@pytest.fixture
def fake_gh():
    gh = FakeGitHub("acme/factory-definition")
    gh.rules["main"] = PR_RULE
    return gh


@pytest.fixture
def auth():
    return FakeAuth()


@pytest.fixture
def sync(store, fake_loader, fake_gh, auth):
    return DefinitionSync(
        settings(), store, auth=auth, loader=fake_loader, client_factory=lambda repo: fake_gh
    )


@pytest.fixture
def local_sync(store, fake_loader):
    return DefinitionSync(settings("/srv/def", mode="personal"), store, loader=fake_loader)


@pytest.fixture
def pat_sync(store, fake_loader, fake_gh):
    return DefinitionSync(
        settings(mode="personal"),
        store,
        auth=PatAuth("ghp_x"),
        loader=fake_loader,
        client_factory=lambda repo: fake_gh,
    )


async def test_new_valid_commit_is_activated(sync, fake_loader, store, events):
    fake_loader.next = LoadResult(sha="b" * 40, source=SRC, definition=VALID, errors=())
    assert await sync.sync_now(reason="push") == "b" * 40 and store.active_sha == "b" * 40
    assert events == ["definition.loaded"]
    assert fake_loader.calls == [(SRC, {"ref": None, "mode": FactoryMode.SERVER})]


async def test_fetch_uses_a_scoped_token_and_hardened_git(sync, fake_loader, auth):
    fake_loader.next = LoadResult(sha="b" * 40, source=SRC, definition=VALID, errors=())
    await sync.sync_now(reason="push")
    assert auth.asked == [("acme/factory-definition", PERMS_FETCH)]
    fetch = fake_loader.fetch_seen[0]
    assert fetch.env["GIT_TOKEN"] == "tok" and fetch.env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "core.hooksPath=/dev/null" in fetch.args and "protocol.allow=never" in fetch.args
    assert GIT_FETCH.get() is None  # never leaks past the call


async def test_same_sha_is_a_no_op(sync, fake_loader):
    fake_loader.next = LoadResult(sha="a" * 40, source=SRC, definition=VALID, errors=())
    assert await sync.sync_now(reason="poll") is None


async def test_invalid_commit_keeps_previous(sync, fake_loader, store):
    fake_loader.next = LoadResult(sha="c" * 40, source=SRC, definition=None, errors=(ERR,))
    assert await sync.sync_now(reason="push") is None and store.active_sha == "a" * 40
    assert store.rows["c" * 40].errors == (str(ERR),)
    assert await sync.sync_now(reason="poll") is None  # a rejected sha is stored once
    assert len(fake_loader.calls) == 2 and len(store.rows) == 1


async def test_unprotected_definition_repo_is_not_activated(sync, fake_loader, store, fake_gh):
    fake_gh.rules["main"] = [{"type": "deletion"}]
    fake_loader.next = LoadResult(sha="b" * 40, source=SRC, definition=VALID, errors=())
    assert await sync.sync_now(reason="push") is None and store.active_sha == "a" * 40
    assert UNPROTECTED_DEFINITION in store.rows["b" * 40].errors[0]


def test_definition_protected():
    assert definition_protected(PR_RULE) is True
    zero = [{"type": "pull_request", "parameters": {"required_approving_review_count": 0}}]
    assert definition_protected(zero) is False
    assert definition_protected([]) is False


async def test_local_source_needs_no_ruleset(local_sync, fake_loader):
    fake_loader.next = LoadResult(sha="b" * 40, source="/srv/def", definition=VALID, errors=())
    assert await local_sync.sync_now(reason="cli") == "b" * 40


def push(ref, repo="acme/factory-definition"):
    return {"ref": ref, "repository": {"full_name": repo}}


def test_push_to_default_branch_only():
    kw = {"definition_repo": "acme/factory-definition", "default_branch": "main"}
    assert definition_hook_payload("push", push("refs/heads/main"), **kw) == {"reason": "push"}
    assert definition_hook_payload("push", push("refs/heads/feature"), **kw) is None
    assert definition_hook_payload("push", push("refs/heads/main", "acme/other"), **kw) is None
    assert (
        definition_hook_payload("push", push("refs/heads/main"), **{**kw, "definition_repo": None})
        is None
    )


@pytest.mark.parametrize(
    "action,want", [("opened", True), ("synchronize", True), ("reopened", True), ("closed", False)]
)
def test_pull_request_actions(action, want):
    body = {
        "action": action,
        "repository": {"full_name": "acme/factory-definition"},
        "pull_request": {"head": {"sha": "c" * 40}},
    }
    got = definition_hook_payload(
        "pull_request", body, definition_repo="acme/factory-definition", default_branch="main"
    )
    assert got == ({"reason": "pull_request", "head_sha": "c" * 40} if want else None)


def test_definition_repo_slug():
    assert definition_repo_slug("https://github.com/acme/def.git") == "acme/def"
    assert definition_repo_slug("https://github.com/acme/def") == "acme/def"
    assert definition_repo_slug("/srv/def") is None and definition_repo_slug(None) is None


async def test_pr_check_run(sync, fake_gh, fake_loader):
    err = DefinitionError("factory.yaml", 12, "apps.demo.merge_policy", "bad")
    fake_loader.next = LoadResult(sha="c" * 40, source=SRC, definition=None, errors=(err,))
    assert await sync.check_pr("acme/factory-definition", "c" * 40) == "failure"
    assert "apps.demo.merge_policy" in fake_gh.check_runs[("c" * 40, DEFINITION_CHECK)]["summary"]
    assert fake_loader.calls[-1][1]["ref"] == "c" * 40


async def test_pr_check_success(sync, fake_gh, fake_loader):
    fake_loader.next = LoadResult(sha="c" * 40, source=SRC, definition=VALID, errors=())
    assert await sync.check_pr("acme/factory-definition", "c" * 40) == "success"
    assert fake_gh.check_runs[("c" * 40, DEFINITION_CHECK)]["conclusion"] == "success"


async def test_check_skipped_with_pat(pat_sync, fake_gh, caplog):
    caplog.set_level(logging.WARNING)
    assert await pat_sync.check_pr("acme/factory-definition", "c" * 40) == "skipped"
    assert caplog.text.count("Checks API") == 1 and fake_gh.check_runs == {}


async def test_concurrent_push_and_poll_sync_once(sync, fake_loader):
    fake_loader.next = LoadResult(sha="b" * 40, source=SRC, definition=VALID, errors=())
    await asyncio.gather(sync.sync_now(reason="push"), sync.sync_now(reason="poll"))
    assert len(fake_loader.calls) == 1
    assert sync.store.rows.keys() == {"b" * 40}  # type: ignore[attr-defined]


async def test_poll_loop_syncs_at_start(sync, fake_loader, store):
    fake_loader.next = LoadResult(sha="b" * 40, source=SRC, definition=VALID, errors=())
    loop = asyncio.create_task(sync.run_poll_loop(interval_s=3600))
    await asyncio.sleep(0.05)
    loop.cancel()
    assert store.active_sha == "b" * 40 and len(fake_loader.calls) == 1


async def test_notification_routes_to_check_or_sync(sync, fake_loader, fake_gh):
    fake_loader.next = LoadResult(sha="c" * 40, source=SRC, definition=VALID, errors=())
    await sync.handle({"reason": "pull_request", "head_sha": "c" * 40})
    assert ("c" * 40, DEFINITION_CHECK) in fake_gh.check_runs
    await sync.handle({"reason": "push"})
    assert sync.store.active_sha == "c" * 40  # type: ignore[attr-defined]


class _Conn:
    def __init__(self):
        self.sent: list[dict] = []

    async def execute(self, stmt, params):
        assert params["ch"] == "definition_sync"
        self.sent.append(__import__("json").loads(params["p"]))


async def _dispatch(event, action, payload):
    from swfactory.github.intake import default_handlers

    conn = _Conn()
    ctx = SimpleNamespace(s=conn, settings=settings())
    handler = default_handlers().get(event, action)
    assert handler is not None
    await handler(ctx, payload)  # pyright: ignore[reportArgumentType]
    return conn.sent


async def test_registered_handlers_only_notify():
    assert await _dispatch("push", "", push("refs/heads/main")) == [{"reason": "push"}]
    assert await _dispatch("push", "", push("refs/heads/feature")) == []
    pr = {
        "action": "synchronize",
        "repository": {"full_name": "acme/factory-definition"},
        "pull_request": {"head": {"sha": "d" * 40}},
    }
    assert await _dispatch("pull_request", "synchronize", pr) == [
        {"reason": "pull_request", "head_sha": "d" * 40}
    ]
