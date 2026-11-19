"""Definition status and the on-demand sync."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from swfactory.api.auth import Principal, require
from swfactory.db.tables import DefinitionRow
from swfactory.definition.loader import load_definition
from swfactory.definition.store import NoActiveDefinition

if TYPE_CHECKING:
    from swfactory.api.app import ApiDeps

router = APIRouter()


class SyncError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


async def sync_definition(deps: ApiDeps) -> tuple[str, dict[str, str]]:
    """Load FACTORY_DEFINITION and make it active (what `factory def sync` does).

    Returns (sha, app changes); raises SyncError when it has no source, does not validate or
    cannot be activated."""
    settings = deps.settings
    source = settings.definition_source
    if not source:
        raise SyncError(["FACTORY_DEFINITION is not set"])
    result = await asyncio.to_thread(
        load_definition, source, ref=settings.definition_ref, mode=settings.mode
    )
    if result.errors:
        raise SyncError([str(e) for e in result.errors])
    if not await deps.definitions.save(result):
        raise SyncError([f"definition {result.sha[:12]} stored but not activated"])
    return result.sha, {}


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


@router.post("/definition/sync")
async def post_sync(
    request: Request, _: Annotated[Principal, Depends(require("admin"))]
) -> dict[str, Any]:
    deps: ApiDeps = request.app.state.deps
    try:
        sha, apps = await sync_definition(deps)
    except SyncError as e:
        raise HTTPException(status_code=422, detail={"errors": e.errors}) from None
    except NoActiveDefinition:
        raise HTTPException(status_code=422, detail={"errors": ["no definition"]}) from None
    return {"active_sha": sha, "apps": apps}
