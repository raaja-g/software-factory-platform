"""A Postgres restart must not wedge the worker.

The checkpointer keeps a pool of connections. After the server restarts (an upgrade, or a
`docker compose up` that recreates it), every idle pooled connection is dead. Without a check on
checkout each one hands out a failure, three failures poison a job, and the worker needed a manual
restart. The pool now validates a connection when it is handed out."""

import psycopg
import pytest

from swfactory.db.engine import open_checkpointer

pytestmark = pytest.mark.db


def _plain(url: str) -> str:
    return url.replace("+psycopg", "", 1)


async def test_checkpointer_survives_its_connections_being_killed(pg_url):
    config = {"configurable": {"thread_id": "reconnect-test"}}
    async with open_checkpointer(pg_url, max_size=4) as saver:
        pool = saver.conn  # the AsyncConnectionPool
        pids: set[int] = set()
        for _ in range(4):  # warm the pool: four idle connections to kill
            async with pool.connection() as conn:  # pyright: ignore[reportAttributeAccessIssue]
                row = await (await conn.execute("select pg_backend_pid() as pid")).fetchone()
                pids.add(row["pid"])
        assert await saver.aget_tuple(config) is None  # works before the "restart"

        async with await psycopg.AsyncConnection.connect(_plain(pg_url), autocommit=True) as admin:
            for pid in pids:
                await admin.execute("select pg_terminate_backend(%s)", (pid,))

        # every pooled connection is now dead; the next uses must not fail
        for _ in range(4):
            assert await saver.aget_tuple(config) is None
