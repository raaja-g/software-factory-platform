"""SqlWebSessionStore against a real database (app.web_sessions)."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.support.pg import execute

from swfactory.api.sessions import SqlWebSessionStore
from swfactory.db.engine import make_engine, make_sessionmaker

pytestmark = pytest.mark.db

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


class Clock:
    def __init__(self) -> None:
        self.t = START

    def __call__(self) -> datetime:
        return self.t

    def advance(self, by: timedelta) -> None:
        self.t += by


@pytest_asyncio.fixture
async def db(pg_url):
    await execute(pg_url, "truncate table app.web_sessions")
    engine = make_engine(pg_url)
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()
        await execute(pg_url, "truncate table app.web_sessions")


async def rows(db):
    async with db() as s:
        result = await s.execute(
            text(
                "select secret_hash, binding, created_at, expires_at, revoked_at, last_seen_at, id "
                "from app.web_sessions order by created_at"
            )
        )
        return result.all()


def sha(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def test_create_stores_only_the_hash_and_the_expiry(db):
    clock = Clock()
    store = SqlWebSessionStore(db, now=clock)
    secret = await store.create("bind", 30 * DAY)
    (row,) = await rows(db)
    assert row.secret_hash == sha(secret) and secret not in "".join(map(str, row))
    assert row.binding == "bind"
    assert row.created_at == START and row.expires_at == START + 30 * DAY
    assert row.revoked_at is None and row.last_seen_at is None
    assert row.id.version == 7


async def test_valid_checks_binding_and_unknown_secrets(db):
    store = SqlWebSessionStore(db, now=Clock())
    secret = await store.create("bind", DAY)
    assert await store.valid(secret, "bind")
    assert not await store.valid(secret, "other-binding")
    assert not await store.valid("never-issued", "bind")
    assert not await store.valid(sha(secret), "bind")  # the stored hash is not a credential


async def test_expiry_is_enforced_by_the_clock(db):
    clock = Clock()
    store = SqlWebSessionStore(db, now=clock)
    secret = await store.create("bind", DAY)
    clock.advance(DAY - timedelta(seconds=1))
    assert await store.valid(secret, "bind")
    clock.advance(timedelta(seconds=1))
    assert not await store.valid(secret, "bind")


async def test_revoke_ends_the_session_and_is_idempotent(db):
    clock = Clock()
    store = SqlWebSessionStore(db, now=clock)
    secret = await store.create("bind", 30 * DAY)
    other = await store.create("bind", 30 * DAY)
    await store.revoke(secret)
    mine = {r.secret_hash: r for r in await rows(db)}[sha(secret)]
    assert mine.revoked_at == START
    clock.advance(timedelta(minutes=5))
    await store.revoke(secret)
    again = {r.secret_hash: r for r in await rows(db)}
    assert again[sha(secret)].revoked_at == START  # the first revocation time stands
    assert again[sha(other)].revoked_at is None
    assert not await store.valid(secret, "bind")
    assert await store.valid(other, "bind")
    await store.revoke("never-issued")


async def test_last_seen_is_written_at_most_once_a_minute(db):
    clock = Clock()
    store = SqlWebSessionStore(db, now=clock)
    secret = await store.create("bind", 30 * DAY)
    assert (await rows(db))[0].last_seen_at is None
    assert await store.valid(secret, "bind")
    assert (await rows(db))[0].last_seen_at == START
    clock.advance(timedelta(seconds=59))
    assert await store.valid(secret, "bind")
    assert (await rows(db))[0].last_seen_at == START
    clock.advance(timedelta(seconds=1))
    assert await store.valid(secret, "bind")
    assert (await rows(db))[0].last_seen_at == START + timedelta(seconds=60)


async def test_the_secret_hash_is_unique(db):
    store = SqlWebSessionStore(db, now=Clock())
    secret = await store.create("bind", DAY)
    async with db() as s, s.begin():
        with pytest.raises(IntegrityError, match="secret_hash"):
            await s.execute(
                text(
                    "insert into app.web_sessions (secret_hash, binding, expires_at) "
                    "values (:h, 'b', now())"
                ),
                {"h": sha(secret)},
            )


async def test_old_expired_and_revoked_rows_are_purged_at_most_once_an_hour(db):
    clock = Clock()
    store = SqlWebSessionStore(db, now=clock)
    expired = await store.create("bind", DAY)
    revoked = await store.create("bind", 60 * DAY)
    live = await store.create("bind", 60 * DAY)
    await store.revoke(revoked)
    clock.advance(timedelta(days=7, minutes=30))  # revoked 7+ days ago; expired 6.98 days ago
    assert await store.valid(live, "bind")
    assert {r.secret_hash for r in await rows(db)} == {sha(expired), sha(live)}
    clock.advance(DAY)
    soon = await store.create("bind", timedelta(minutes=1))
    clock.advance(timedelta(days=7))  # `soon` has been expired for just under 7 days
    assert await store.valid(live, "bind")  # purge due: `expired` goes, `soon` stays
    assert {r.secret_hash for r in await rows(db)} == {sha(live), sha(soon)}
    clock.advance(timedelta(minutes=2))  # `soon` is purgeable, but the last purge was 2 min ago
    assert await store.valid(live, "bind")
    assert sha(soon) in {r.secret_hash for r in await rows(db)}
    clock.advance(timedelta(minutes=60))
    assert await store.valid(live, "bind")
    assert {r.secret_hash for r in await rows(db)} == {sha(live)}


async def test_the_api_uses_the_database_store_end_to_end(db):
    from fastapi.testclient import TestClient
    from tests.unit.api.api_support import fake_deps, settings

    from swfactory.api.app import create_app

    ui = "http://127.0.0.1:7300"

    def app_client(**kw):
        kw.setdefault("FACTORY_LOGIN_PASSWORD", "correct horse battery")
        s = settings(
            FACTORY_LOGIN_USER="raja", FACTORY_PUBLIC_URL=ui, FACTORY_SERVE_WEB="false", **kw
        )
        deps = fake_deps(s, web_sessions=SqlWebSessionStore(db))
        app = create_app(s, deps, on_gce=lambda: False)  # pyright: ignore[reportArgumentType]
        return TestClient(app, base_url="http://127.0.0.1:7080")

    body = {"username": "raja", "password": "correct horse battery"}
    c = app_client()
    assert c.post("/api/login", json=body, headers={"origin": ui}).status_code == 200
    secret = c.cookies["sf_session"]
    assert [r.secret_hash for r in await rows(db)] == [sha(secret)]
    assert c.get("/api/me").json()["via"] == "cookie"
    changed = app_client(FACTORY_LOGIN_PASSWORD="a different password")
    changed.cookies.set("sf_session", secret)
    assert changed.get("/api/me").status_code == 401  # the login changed
    assert c.post("/api/logout", headers={"origin": ui}).status_code == 200
    replay = app_client()
    replay.cookies.set("sf_session", secret)
    assert replay.get("/api/me").status_code == 401  # revoked in the database
    assert (await rows(db))[0].revoked_at is not None
