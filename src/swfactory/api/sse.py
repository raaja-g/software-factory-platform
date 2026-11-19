"""Server-Sent Events: GET /api/events with Last-Event-ID resume and an app filter."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Collection
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from swfactory.api.access import visible_apps
from swfactory.api.auth import Principal, require
from swfactory.definition.store import NoActiveDefinition
from swfactory.events import Event, EventBus

router = APIRouter()


def format_sse(event: Event) -> str:
    payload = {**event.data, "job_id": str(event.job_id) if event.job_id is not None else None}
    data = json.dumps(payload, sort_keys=True, separators=(", ", ": "), default=str)
    return f"id: {event.id}\nevent: {event.kind}\ndata: {data}\n\n"


async def event_stream(
    bus: EventBus,
    *,
    app_slug: str | None,
    after_id: int | None,
    ping_s: float = 15.0,
    app_slugs: Collection[str] | None = None,
) -> AsyncGenerator[str]:
    """Yield formatted events, and a comment ping whenever nothing arrives for ping_s."""
    sub = bus.subscribe(app_slug=app_slug, after_id=after_id, app_slugs=app_slugs)
    pending: asyncio.Task[Event] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(sub))
            done, _ = await asyncio.wait({pending}, timeout=ping_s)
            if not done:
                yield ": ping\n\n"
                continue
            task, pending = pending, None
            try:
                event = task.result()
            except StopAsyncIteration:
                return
            yield format_sse(event)
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        aclose = getattr(sub, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


def _parse_id(value: str, what: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"bad {what}") from None
    if n < 0:
        raise HTTPException(status_code=400, detail=f"bad {what}")
    return n


@router.get("/events")
async def events(
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
    app: str | None = None,
    after: int | None = None,
) -> StreamingResponse:
    header = request.headers.get("last-event-id")
    after_id = _parse_id(header, "Last-Event-ID") if header is not None else after
    deps = request.app.state.deps
    bus: Any = deps.events
    visible = None
    try:
        _, defn = await deps.definitions.active()
        visible = visible_apps(defn, principal, mode=request.app.state.mode)
    except NoActiveDefinition:
        pass
    if visible is not None and app and app not in visible:
        raise HTTPException(status_code=404, detail="app not found")

    async def body() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        stream = event_stream(bus, app_slug=app or None, after_id=after_id, app_slugs=visible)
        try:
            async for chunk in stream:
                if await request.is_disconnected():
                    return
                yield chunk
        finally:
            await stream.aclose()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
