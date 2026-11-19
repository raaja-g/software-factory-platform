import os
import uuid

import pytest
import pytest_asyncio
from tests.support.pg import truncate_app_tables

from swfactory.db.engine import make_engine, make_sessionmaker, setup_checkpointer
from swfactory.db.migrate import upgrade_database
from swfactory.settings import get_settings


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch):
    monkeypatch.setenv(
        "FACTORY_DATABASE_URL", "postgresql+psycopg://factory@127.0.0.1:5432/factory"
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _db_url_with(url: str, dbname: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).set(database=dbname).render_as_string(hide_password=False)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pg_url():
    base = os.environ.get("FACTORY_TEST_DATABASE_URL")
    if not base:
        pytest.skip("set FACTORY_TEST_DATABASE_URL")
    name = f"factory_test_{uuid.uuid4().hex[:8]}"
    await _admin(base, f'create database "{name}"')
    try:
        url = _db_url_with(base, name)
        upgrade_database(url)
        await setup_checkpointer(url)
        yield url
    finally:
        await _admin(base, f'drop database if exists "{name}" with (force)')


async def _admin(url: str, sql: str) -> None:
    engine = make_engine(url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            from sqlalchemy import text

            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sessions(pg_url):
    engine = make_engine(pg_url)
    try:
        async with engine.connect() as conn:
            await truncate_app_tables(conn)
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def api_server(pg_url):
    """A real `factory serve` on a free port, in personal mode, on the test database."""
    import subprocess
    import sys
    import time

    import httpx

    port, token = _free_port(), "sfp_cli-test-token"
    env = {k: v for k, v in os.environ.items() if k != "FACTORY_DATABASE_PASSWORD_FILE"}
    env.update(FACTORY_DATABASE_URL=pg_url, FACTORY_MODE="personal", FACTORY_API_TOKEN=token)
    proc = subprocess.Popen(
        [sys.executable, "-c", "from swfactory.cli.main import app; app()"]
        + ["serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        end = time.monotonic() + 30
        while time.monotonic() < end:
            try:
                if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise AssertionError("factory serve did not start")
        yield url, token
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture
def api_env(api_server, pg_url, monkeypatch):
    """Point the CLI at the live test API and the test database."""
    url, token = api_server
    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    monkeypatch.delenv("FACTORY_DATABASE_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("FACTORY_MODE", "personal")
    monkeypatch.setenv("FACTORY_API_URL", url)
    monkeypatch.setenv("FACTORY_API_TOKEN", token)
    get_settings.cache_clear()
    return url
