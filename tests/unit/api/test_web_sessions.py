"""Dashboard sessions are random, stored (as a hash) on the server, expire there, end at logout
and stop working when the login user, password or API token changes."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.api.api_support import PERSONAL_TOKEN, settings
from tests.unit.api.test_auth import bearer, make_client

import swfactory.api.app as app_mod

UI = "http://127.0.0.1:7000"
PW = "correct horse battery"
USER = "Raja"
START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


class Clock:
    def __init__(self) -> None:
        self.t = START

    def __call__(self) -> datetime:
        return self.t

    def advance(self, by: timedelta) -> None:
        self.t += by


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    async def instant(_s):
        return None

    monkeypatch.setattr(app_mod.asyncio, "sleep", instant)


def make_store(clock=None):
    from swfactory.api.sessions import InMemoryWebSessionStore

    return InMemoryWebSessionStore(now=clock or Clock())


def conf(**kw):
    kw.setdefault("FACTORY_LOGIN_USER", USER)
    kw.setdefault("FACTORY_LOGIN_PASSWORD", PW)
    return settings(FACTORY_PUBLIC_URL=UI, FACTORY_SERVE_WEB="false", **kw)


def client(store=None, **kw):
    if store is None:
        return make_client(conf(**kw))
    return make_client(conf(**kw), web_sessions=store)


def login(c, password=PW, username=USER):
    r = c.post(
        "/api/login",
        json={"username": username, "password": password},
        headers={"origin": UI},
    )
    assert r.status_code == 200, r.text
    return c.cookies["sf_session"]


def link_login(c):
    code = c.post("/api/login-code", headers=bearer(PERSONAL_TOKEN)).json()["code"]
    r = c.get(f"/login?code={code}", follow_redirects=False)
    assert r.status_code == 303
    return c.cookies["sf_session"]


def with_cookie(value, store=None, **kw):
    c = client(store, **kw)
    c.cookies.set("sf_session", value)
    return c


# ---- the cookie is a random secret ----


def test_the_cookie_is_different_on_every_login_and_not_derivable_from_the_token():
    first = login(client())
    second = login(client())
    assert first != second
    old_style = hmac.new(PERSONAL_TOKEN.encode(), b"session", hashlib.sha256).hexdigest()
    forbidden = {old_style, PERSONAL_TOKEN, hashlib.sha256(PERSONAL_TOKEN.encode()).hexdigest()}
    assert not ({first, second} & forbidden)
    assert len(first) >= 43  # 32 random bytes, url-safe


def test_the_one_time_link_login_also_gets_a_random_cookie():
    a, b = link_login(client()), link_login(client())
    assert a != b and len(a) >= 43


def test_both_login_paths_make_sessions_the_same_way():
    store = make_store()
    c = client(store)
    by_password = login(c)
    by_link = link_login(client(store))
    assert len(store.rows) == 2
    for secret in (by_password, by_link):
        assert with_cookie(secret, store).get("/api/me").json()["via"] == "cookie"


# ---- the server keeps only a hash ----


def test_the_store_keeps_a_hash_never_the_secret():
    store = make_store()
    secret = login(client(store))
    assert hashlib.sha256(secret.encode()).hexdigest() in store.rows
    assert secret not in repr(store.rows) and secret not in store.rows


# ---- expiry on the server ----


def test_an_expired_session_is_refused_even_if_the_browser_still_sends_it():
    clock = Clock()
    store = make_store(clock)
    secret = login(client(store))
    c = with_cookie(secret, store)
    clock.advance(30 * DAY - timedelta(seconds=1))
    assert c.get("/api/me").status_code == 200
    clock.advance(timedelta(seconds=1))  # now == expires_at
    assert c.get("/api/me").status_code == 401


def test_expiry_follows_the_session_days_setting_and_matches_max_age():
    clock = Clock()
    store = make_store(clock)
    c = client(store, FACTORY_SESSION_DAYS="7")
    r = c.post("/api/login", json={"username": USER, "password": PW}, headers={"origin": UI})
    assert f"max-age={7 * 86400}" in r.headers["set-cookie"].lower()
    (row,) = store.rows.values()
    assert row.created_at == START
    assert row.expires_at == START + 7 * DAY


# ---- logout ends the session on the server ----


def test_logout_revokes_the_session_so_a_copied_cookie_stops_working():
    store = make_store()
    c = client(store)
    secret = login(c)
    thief = with_cookie(secret, store)
    assert thief.get("/api/me").status_code == 200
    r = c.post("/api/logout", headers={"origin": UI})
    assert r.status_code == 200 and "max-age=0" in r.headers["set-cookie"].lower()
    assert thief.get("/api/me").status_code == 401  # replayed on a fresh client
    (row,) = store.rows.values()
    assert row.revoked_at is not None


def test_logout_ends_only_that_session():
    store = make_store()
    one, two = client(store), client(store)
    login(one)
    login(two)
    one.post("/api/logout", headers={"origin": UI})
    assert one.get("/api/me").status_code == 401
    assert two.get("/api/me").status_code == 200


def test_logout_without_a_cookie_or_with_junk_still_succeeds():
    c = client()
    assert c.post("/api/logout", headers={"origin": UI}).status_code == 200
    c.cookies.set("sf_session", "junk")
    assert c.post("/api/logout", headers={"origin": UI}).status_code == 200


# ---- a changed login ends every session ----


@pytest.mark.parametrize(
    "changed",
    [
        {"FACTORY_LOGIN_USER": "someone-else"},
        {"FACTORY_LOGIN_PASSWORD": "another long password"},
        {"FACTORY_API_TOKEN": "sfp_a_different_token"},
    ],
    ids=["user", "password", "token"],
)
def test_changing_the_login_invalidates_existing_sessions(changed):
    store = make_store()
    secret = login(client(store))
    assert with_cookie(secret, store).get("/api/me").status_code == 200
    restarted = with_cookie(secret, store, **changed)
    assert restarted.get("/api/me").status_code == 401


def test_removing_the_login_password_invalidates_sessions():
    store = make_store()
    secret = login(client(store))
    c = make_client(settings(FACTORY_LOGIN_USER=USER, FACTORY_PUBLIC_URL=UI), web_sessions=store)
    c.cookies.set("sf_session", secret)
    assert c.get("/api/me").status_code == 401


def test_the_login_user_case_does_not_matter_for_the_binding():
    store = make_store()
    secret = login(client(store))
    same_user = with_cookie(secret, store, FACTORY_LOGIN_USER=USER.upper())
    assert same_user.get("/api/me").status_code == 200


def test_the_binding_is_an_hmac_of_user_and_password_keyed_by_the_token():
    from swfactory.api.sessions import session_binding

    s = conf()
    want = hmac.new(
        PERSONAL_TOKEN.encode(), f"{USER.lower()}\x00{PW}".encode(), hashlib.sha256
    ).hexdigest()
    assert session_binding(s, PERSONAL_TOKEN) == want
    bare = settings()  # no login user or password: the local user and an empty password
    want_bare = hmac.new(PERSONAL_TOKEN.encode(), b"local\x00", hashlib.sha256).hexdigest()
    assert session_binding(bare, PERSONAL_TOKEN) == want_bare


# ---- bad cookies are 401 ----


def test_bad_cookies_are_refused():
    store = make_store()
    secret = login(client(store))
    old_format = hmac.new(PERSONAL_TOKEN.encode(), b"session", hashlib.sha256).hexdigest()
    flipped = secret[:-1] + ("A" if secret[-1] != "A" else "B")
    for bad in ("", "forged", old_format, secret[:-1], secret[:10], flipped, secret + "x"):
        c = with_cookie(bad, store)
        assert c.get("/api/me").status_code == 401, bad
    assert with_cookie(secret, store).get("/api/me").status_code == 200


def test_the_old_fixed_cookie_value_no_longer_works_even_with_an_empty_store():
    old_format = hmac.new(PERSONAL_TOKEN.encode(), b"session", hashlib.sha256).hexdigest()
    assert with_cookie(old_format).get("/api/me").status_code == 401


def test_the_bearer_token_still_works_without_any_session():
    assert client().get("/api/me", headers=bearer(PERSONAL_TOKEN)).json()["via"] == "token"


# ---- store behaviour ----


async def test_last_seen_is_written_at_most_once_a_minute():
    clock = Clock()
    store = make_store(clock)
    secret = await store.create("b", timedelta(days=30))
    (row,) = store.rows.values()
    assert row.last_seen_at is None
    assert await store.valid(secret, "b")
    assert row.last_seen_at == START
    clock.advance(timedelta(seconds=59))
    assert await store.valid(secret, "b")
    assert row.last_seen_at == START  # no write
    clock.advance(timedelta(seconds=1))
    assert await store.valid(secret, "b")
    assert row.last_seen_at == START + timedelta(seconds=60)


async def test_valid_needs_the_same_binding_and_a_live_row():
    clock = Clock()
    store = make_store(clock)
    secret = await store.create("b", timedelta(days=1))
    assert not await store.valid(secret, "other")
    assert not await store.valid("nope", "b")
    await store.revoke(secret)
    assert not await store.valid(secret, "b")
    await store.revoke("never-issued")  # a no-op, not an error


def key(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


async def test_old_expired_and_revoked_rows_are_purged_and_live_ones_stay():
    clock = Clock()
    store = make_store(clock)
    expired = await store.create("b", timedelta(days=1))
    revoked = await store.create("b", timedelta(days=60))
    live = await store.create("b", timedelta(days=60))
    await store.revoke(revoked)
    assert len(store.rows) == 3
    clock.advance(timedelta(days=7, minutes=30))  # revoked 7+ days ago, expired 6.98 days ago
    await store.valid(live, "b")
    assert key(expired) in store.rows  # expired for less than 7 days: kept
    clock.advance(timedelta(days=1))
    await store.valid(live, "b")
    assert set(store.rows) == {key(live)}  # both old rows are gone


async def test_a_row_expired_for_under_a_week_is_kept_then_purged():
    clock = Clock()
    store = make_store(clock)
    gone = await store.create("b", timedelta(days=1))
    clock.advance(timedelta(days=7))  # expired 6 days
    keeper = await store.create("b", timedelta(days=1))
    assert key(gone) in store.rows
    clock.advance(timedelta(days=2))  # expired 8 days
    await store.valid(keeper, "b")
    assert key(gone) not in store.rows


async def test_the_purge_runs_at_most_once_an_hour():
    clock = Clock()
    store = make_store(clock)
    await store.create("b", timedelta(days=1))  # a purge runs here (first call)
    clock.advance(timedelta(days=8, minutes=10))
    live = await store.create("b", timedelta(days=60))  # purge due: the first row goes
    assert len(store.rows) == 1
    soon = await store.create("b", timedelta(minutes=1))
    clock.advance(timedelta(days=7))
    await store.valid(live, "b")  # purge due, but `soon` is not yet 7 days old
    assert key(soon) in store.rows
    clock.advance(timedelta(minutes=2))  # `soon` is now purgeable, the last purge was 2 min ago
    await store.valid(live, "b")
    assert key(soon) in store.rows
    clock.advance(timedelta(minutes=60))
    await store.valid(live, "b")
    assert key(soon) not in store.rows


def test_production_wiring_builds_a_database_store():
    from swfactory.api.sessions import SqlWebSessionStore
    from swfactory.domain.types import FactoryMode

    deps, _engine = app_mod._build_deps(settings(), FactoryMode.PERSONAL)
    assert isinstance(deps.web_sessions, SqlWebSessionStore)
