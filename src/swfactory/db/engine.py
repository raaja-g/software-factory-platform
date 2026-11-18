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


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


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
async def open_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    pool = AsyncConnectionPool(
        _conninfo(database_url), kwargs=checkpointer_pool_kwargs(), open=False
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
