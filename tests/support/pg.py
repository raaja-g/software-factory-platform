from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from swfactory.db.engine import make_engine


async def truncate_app_tables(conn: AsyncConnection) -> list[str]:
    db = await conn.scalar(text("select current_database()"))
    if not str(db).startswith("factory_test_"):
        raise RuntimeError(f"refusing to truncate: {db!r} is not a factory_test_ database")
    rows = await conn.execute(
        text(
            "select tablename from pg_tables where schemaname = 'app' "
            "and tablename <> 'alembic_version' order by tablename"
        )
    )
    names = [r[0] for r in rows]
    if names:
        listed = ", ".join(f'app."{n}"' for n in names)
        await conn.execute(text(f"truncate table {listed} restart identity cascade"))
        await conn.commit()
    return names


async def scalar(url: str, sql: str) -> Any:
    engine = make_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.scalar(text(sql))
    finally:
        await engine.dispose()


async def execute(url: str, sql: str) -> None:
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


def libpq(url: str) -> str:
    return url.replace("+psycopg", "", 1)
