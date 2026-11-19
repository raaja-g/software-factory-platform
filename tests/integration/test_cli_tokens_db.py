import asyncio
import re
import uuid

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.db.engine import make_engine
from swfactory.settings import get_settings

pytestmark = pytest.mark.db


@pytest.fixture
async def cli_db(pg_url, monkeypatch):
    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    monkeypatch.delenv("FACTORY_DATABASE_PASSWORD_FILE", raising=False)
    get_settings.cache_clear()
    engine = make_engine(pg_url)
    async with engine.begin() as c:
        await c.execute(text("delete from app.api_tokens"))
        await c.execute(text("delete from app.job_events where kind = 'admin.changed'"))
    loop = asyncio.get_running_loop()

    async def query(sql, *args):
        async with engine.connect() as c:
            params = {f"p{i}": a for i, a in enumerate(args)}
            for i in range(len(args)):
                sql = sql.replace("%s", f":p{i}", 1)
            return (await c.execute(text(sql), params)).all()

    class Cli:
        async def __call__(self, *args):
            return await loop.run_in_executor(None, lambda: CliRunner().invoke(app, list(args)))

        async def scalar(self, sql, *args):
            return (await query(sql, *args))[0][0]

        async def event_data(self, kind):
            rows = await query("select data from app.job_events where kind = %s order by id", kind)
            return [r[0] for r in rows]

    yield Cli()
    await engine.dispose()


async def test_tokens_create_list_revoke(cli_db):
    out = (
        await cli_db(
            "tokens", "create", "--email", "a@x.io", "--name", "laptop", "--by", "admin@x.io"
        )
    ).output
    m = re.search(r"id ([0-9a-f-]{36})", out)
    assert m
    tid = m[1]
    assert "token sfp_" in out
    q = "select expires_at::date - created_at::date from app.api_tokens where id = %s"
    assert await cli_db.scalar(q, tid) == 90
    listed = (await cli_db("tokens", "list")).output
    assert "laptop" in listed and "sfp_" not in listed
    assert (await cli_db("tokens", "revoke", tid, "--by", "admin@x.io")).exit_code == 0
    q = "select revoked_at is not null from app.api_tokens where id = %s"
    assert await cli_db.scalar(q, tid) is True
    assert await cli_db.event_data("admin.changed") == [
        {"what": "token.create", "target": tid, "by": "admin@x.io"},
        {"what": "token.revoke", "target": tid, "by": "admin@x.io"},
    ]


async def test_tokens_create_zero_days_never_expires(cli_db):
    out = (
        await cli_db("tokens", "create", "--email", "a@x.io", "--name", "n", "--days", "0")
    ).output
    m = re.search(r"id ([0-9a-f-]{36})", out)
    assert m
    tid = m[1]
    assert await cli_db.scalar("select expires_at is null from app.api_tokens where id = %s", tid)


async def test_tokens_revoke_unknown_exits_1(cli_db):
    assert (await cli_db("tokens", "revoke", str(uuid.uuid4()))).exit_code == 1
