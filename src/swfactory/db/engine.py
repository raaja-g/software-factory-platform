from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from swfactory.gateway.render import LITELLM_DB_POOL_LIMIT
from swfactory.settings import Settings

PG_MAX_CONNECTIONS = 100
_ADMIN_RESERVE = 10  # migrate, backups and admin shells


def make_engine(
    url: str, *, pool_size: int = 5, max_overflow: int = 5, statement_timeout_s: int = 0
) -> AsyncEngine:
    kwargs: dict[str, Any] = {}
    if statement_timeout_s > 0:
        kwargs["connect_args"] = {"options": f"-c statement_timeout={statement_timeout_s * 1000}"}
    return create_async_engine(
        url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow, **kwargs
    )


def connection_budget(settings: Settings) -> int:
    """Postgres connections the default stack can open: one api, one worker, LiteLLM, admin."""
    pool = settings.db_pool_size + settings.db_max_overflow
    api = pool + 1  # LISTEN
    worker = pool + settings.checkpointer_pool_max + 2  # LISTEN: commands, definition sync
    return api + worker + LITELLM_DB_POOL_LIMIT + _ADMIN_RESERVE


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


DURABILITY = "sync"


def checkpointer_pool_kwargs() -> dict[str, Any]:
    """Connection kwargs for the checkpointer pool; tables live in schema ``lg`` (R01)."""
    return {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
        "options": "-c search_path=lg",
    }


def _conninfo(database_url: str) -> str:
    return database_url.replace("+psycopg", "", 1)


@asynccontextmanager
async def open_checkpointer(
    database_url: str, *, max_size: int = 4
) -> AsyncIterator[AsyncPostgresSaver]:
    pool = AsyncConnectionPool(
        _conninfo(database_url), kwargs=checkpointer_pool_kwargs(), max_size=max_size, open=False
    )
    await pool.open()
    try:
        yield AsyncPostgresSaver(pool)  # pyright: ignore[reportArgumentType]
    finally:
        await pool.close()


async def setup_checkpointer(database_url: str) -> None:
    """Create schema ``lg`` and the checkpoint tables. Only migrations call this, never workers."""
    async with await AsyncConnection.connect(_conninfo(database_url), autocommit=True) as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS lg")
    async with open_checkpointer(database_url) as saver:
        await saver.setup()
