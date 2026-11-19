"""Definition status and the on-demand sync (a notification; the worker does the sync)."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text

from swfactory.api.auth import Principal, require
from swfactory.db.tables import DefinitionRow
from swfactory.definition.sync import CHANNEL
from swfactory.events import stream_position

if TYPE_CHECKING:
    from swfactory.api.app import ApiDeps

router = APIRouter()


async def notify_sync(deps: ApiDeps, payload: dict[str, Any]) -> None:
    """Ask the orchestrator to sync: the api never writes `definitions` or `apps` itself."""
    async with deps.sessions() as s, s.begin():
        await s.execute(
            text("select pg_notify(:ch, :p)"), {"ch": CHANNEL, "p": json.dumps(payload)}
        )


async def stream_position_now(deps: ApiDeps) -> int:
    async with deps.sessions() as s:
        return await stream_position(s)


async def wait_loaded(deps: ApiDeps, sha: str, *, after_id: int, timeout_s: float) -> bool:
    """True once a `definition.loaded` event for ``sha`` arrives after ``after_id``."""

    async def scan() -> None:
        async for ev in deps.events.subscribe(after_id=after_id):
            if ev.kind == "definition.loaded" and ev.data.get("sha") == sha:
                return

    try:
        await asyncio.wait_for(scan(), timeout_s)
    except TimeoutError:
        return False
    return True


async def definition_status(deps: ApiDeps) -> dict[str, Any]:
    active_sha: str | None = None
    source: str | None = None
    loaded_at: str | None = None
    async with deps.sessions() as s:
        active = await s.scalar(select(DefinitionRow).where(DefinitionRow.active.is_(True)))
        if active is not None:
            active_sha, source, loaded_at = active.sha, active.source, active.loaded_at.isoformat()
        rejected = await s.scalar(
            select(DefinitionRow)
            .where(func.jsonb_array_length(DefinitionRow.errors) > 0)
            .order_by(DefinitionRow.loaded_at.desc())
            .limit(1)
        )
    last_rejected = (
        {"sha": rejected.sha, "errors": list(rejected.errors)} if rejected is not None else None
    )
    return {
        "active_sha": active_sha,
        "source": source,
        "loaded_at": loaded_at,
        "last_rejected": last_rejected,
    }


@router.get("/definition")
async def get_definition(
    request: Request, _: Annotated[Principal, Depends(require("viewer"))]
) -> dict[str, Any]:
    return await definition_status(request.app.state.deps)


@router.post("/definition/sync", status_code=202)
async def post_sync(
    request: Request, _: Annotated[Principal, Depends(require("admin"))]
) -> dict[str, Any]:
    deps: ApiDeps = request.app.state.deps
    await notify_sync(deps, {"reason": "api"})
    return {"status": "requested"}
