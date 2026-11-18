import os

import pytest
from sqlalchemy import text
from tests.support.pg import scalar, truncate_app_tables

from swfactory.db.engine import make_engine

pytestmark = pytest.mark.db


async def test_pg_url_is_isolated_pg18(pg_url):
    assert (await scalar(pg_url, "select current_database()")).startswith("factory_test_")
    assert int(await scalar(pg_url, "show server_version_num")) >= 180000
    assert await scalar(pg_url, "show server_encoding") == "UTF8"


async def test_truncate_refuses_non_test_database(pg_url):
    engine = make_engine(os.environ["FACTORY_TEST_DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            with pytest.raises(RuntimeError, match="not a factory_test_ database"):
                await truncate_app_tables(conn)
            assert await conn.scalar(text("select current_database()")) == "postgres"
    finally:
        await engine.dispose()


async def test_sessions_fixture_truncates(sessions):
    async with sessions() as s:
        assert await s.scalar(text("select 1")) == 1
