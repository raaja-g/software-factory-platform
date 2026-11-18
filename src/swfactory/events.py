"""Event bus: append-only job events, LISTEN/NOTIFY wake-ups, polling fallback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.db.tables import JobEvent

log = logging.getLogger(__name__)

EVENT_KINDS = frozenset(
    {
        "job.created",
        "job.stage",
        "run.started",
        "run.finished",
        "review.verdict",
        "decision.made",
        "human.waiting",
        "human.action",
        "delivery.pr_opened",
        "delivery.rebased",
        "delivery.merged",
        "cost.updated",
        "score.recorded",
        "job.failed",
        "job.stuck",
        "job.migrated",
        "definition.loaded",
        "model.fallback",
        "improvement.updated",
        "delivery.closed",
        "job.superseded",
        "job.expired",
        "sandbox.network",
        "models.refresh_requested",
        "models.refreshed",
        "admin.changed",
        "run.progress",
        "factory.halted",
        "factory.resumed",
        "backlog.created",
        "backlog.approved",
        "backlog.paused",
        "backlog.resumed",
        "backlog.done",
        "backlog.cancelled",
    }
)

_BATCH = 500
_OLDEST_OPEN = "pg_snapshot_xmin(pg_current_snapshot())"


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    job_id: UUID | None
    at: datetime
    kind: str
    data: Mapping[str, Any]


async def record_event(
    s: AsyncSession, job_id: UUID | None, kind: str, data: Mapping[str, Any]
) -> int:
    """Insert an event in the caller's transaction; does not commit."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind '{kind}'")
    payload = json.loads(json.dumps(dict(data), default=str))
    result = await s.execute(
        insert(JobEvent).values(job_id=job_id, kind=kind, data=payload).returning(JobEvent.id)
    )
    return int(result.scalar_one())


async def stream_position(s: AsyncSession) -> int:
    """Id of the last event, in stream order, older than every open transaction."""
    row = await s.scalar(
        text(
            "select id from app.job_events "
            f"where xid < {_OLDEST_OPEN} order by xid desc, id desc limit 1"
        )
    )
    return int(row) if row is not None else 0


Cursor = tuple[str, int]  # (xid as text, id)


async def _cursor_of(s: AsyncSession, event_id: int) -> Cursor:
    if event_id <= 0:
        return ("0", 0)
    row = (
        await s.execute(
            text(
                "select xid::text, id from app.job_events where id <= :i order by id desc limit 1"
            ),
            {"i": event_id},
        )
    ).first()
    if row is None:
        return ("0", 0)
    return (row[0], int(row[1]))


class EventBus:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None,
        dsn: str,
        *,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._sessions = sessions
        self._dsn = dsn
        self._poll = poll_interval_s
        self._cond = asyncio.Condition()
        self._gen = 0
        self._subscribers = 0
        self._listener: asyncio.Task[None] | None = None
        self._warned = False

    async def emit(self, job_id: UUID | None, kind: str, data: Mapping[str, Any]) -> None:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind '{kind}'")
        assert self._sessions is not None
        async with self._sessions() as s, s.begin():
            await record_event(s, job_id, kind, data)

    # -- listener shared by all subscribers --------------------------------

    async def _wake(self) -> None:
        async with self._cond:
            self._gen += 1
            self._cond.notify_all()

    async def _listen(self) -> None:
        conn: psycopg.AsyncConnection[Any] | None = None
        try:
            while True:
                if conn is None:
                    try:
                        conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
                        await conn.execute("LISTEN job_events")
                    except Exception as exc:
                        if conn is not None:
                            with contextlib.suppress(Exception):
                                await conn.close()
                        conn = None
                        if not self._warned:
                            self._warned = True
                            log.warning("event bus LISTEN unavailable, polling only: %s", exc)
                if conn is None:
                    await asyncio.sleep(self._poll)
                else:
                    try:
                        async for _ in conn.notifies(timeout=self._poll):
                            await self._wake()
                    except Exception as exc:
                        log.warning("event bus LISTEN connection lost: %s", exc)
                        with contextlib.suppress(Exception):
                            await conn.close()
                        conn = None
                        await asyncio.sleep(self._poll)
                await self._wake()
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()

    def _join(self) -> None:
        self._subscribers += 1
        if self._listener is None:
            self._listener = asyncio.ensure_future(self._listen())

    async def _leave(self) -> None:
        self._subscribers -= 1
        if self._subscribers == 0 and self._listener is not None:
            task, self._listener = self._listener, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- subscribe ----------------------------------------------------------

    async def _fetch(
        self, cursor: Cursor, app_slug: str | None, app_slugs: Collection[str] | None
    ) -> list[tuple[Cursor, Event]]:
        assert self._sessions is not None
        where = [
            "(e.xid, e.id) > (cast(:x as xid8), :i)",
            f"e.xid < {_OLDEST_OPEN}",
        ]
        params: dict[str, Any] = {"x": cursor[0], "i": cursor[1], "n": _BATCH}
        join = ""
        if app_slug is not None or app_slugs is not None:
            join = "left join app.jobs j on j.id = e.job_id left join app.apps a on a.id = j.app_id"
        if app_slug is not None:
            where.append("a.slug = :slug")
            params["slug"] = app_slug
        elif app_slugs is not None:
            where.append("(e.job_id is null or a.slug = any(:slugs))")
            params["slugs"] = list(app_slugs)
        sql = (
            "select e.xid::text, e.id, e.job_id, e.at, e.kind, e.data "
            f"from app.job_events e {join} where {' and '.join(where)} "
            "order by e.xid, e.id limit :n"
        )
        async with self._sessions() as s:
            rows = (await s.execute(text(sql), params)).all()
        return [((r[0], int(r[1])), Event(int(r[1]), r[2], r[3], r[4], r[5] or {})) for r in rows]

    async def subscribe(
        self,
        *,
        app_slug: str | None = None,
        after_id: int | None = None,
        app_slugs: Collection[str] | None = None,
    ) -> AsyncGenerator[Event]:
        assert self._sessions is not None
        self._join()
        try:
            async with self._sessions() as s:
                if after_id is None:
                    after_id = await stream_position(s)
                cursor = await _cursor_of(s, after_id)
            while True:
                gen = self._gen
                rows = await self._fetch(cursor, app_slug, app_slugs)
                for cur, ev in rows:
                    cursor = cur
                    yield ev
                if len(rows) >= _BATCH:
                    continue
                async with self._cond:
                    if self._gen == gen:
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._cond.wait(), self._poll)
        finally:
            await self._leave()
