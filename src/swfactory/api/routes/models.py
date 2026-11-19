"""Model catalog routes: list, request an on-demand refresh, poll it."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from swfactory.api.auth import Principal, require
from swfactory.events import record_event
from swfactory.models.catalog import ModelInfo

if TYPE_CHECKING:
    from swfactory.api.app import ApiDeps

router = APIRouter()


def model_json(m: ModelInfo) -> dict[str, Any]:
    out = asdict(m)
    for k in ("price_in_per_mtok", "price_out_per_mtok"):
        out[k] = str(Decimal(out[k]))
    return out


class RefreshRequest(BaseModel):
    aliases: list[str] | None = None


async def refreshed_event(deps: ApiDeps, request_id: int) -> dict[str, Any] | None:
    """The ``models.refreshed`` data answering that request, or None while it is open."""
    async with deps.sessions() as s:
        row = (
            await s.execute(
                text(
                    "select data from app.job_events where job_id is null "
                    "and kind = 'models.refreshed' and (data->>'request_id')::bigint = :r "
                    "order by id desc limit 1"
                ),
                {"r": request_id},
            )
        ).first()
    return None if row is None else dict(row[0])


@router.get("/models")
async def list_models(
    request: Request,
    _: Annotated[Principal, Depends(require("viewer"))],
    available: bool | None = None,
) -> list[dict[str, Any]]:
    deps: ApiDeps = request.app.state.deps
    models = await deps.catalog.list(available_only=available is True)
    if available is False:
        models = [m for m in models if not m.available]
    return [model_json(m) for m in models]


@router.post("/models/refresh", status_code=202)
async def request_refresh(
    request: Request,
    principal: Annotated[Principal, Depends(require("admin"))],
    body: RefreshRequest | None = None,
) -> dict[str, int]:
    deps: ApiDeps = request.app.state.deps
    aliases = body.aliases if body is not None else None
    async with deps.sessions() as s, s.begin():
        rid = await record_event(
            s, None, "models.refresh_requested", {"aliases": aliases, "by": principal.email}
        )
    return {"request_id": rid}


@router.get("/models/refresh/{request_id}", response_model=None)
async def refresh_status(
    request_id: int, request: Request, _: Annotated[Principal, Depends(require("admin"))]
) -> JSONResponse | dict[str, Any]:
    deps: ApiDeps = request.app.state.deps
    done = await refreshed_event(deps, request_id)
    if done is None:
        return {"done": False}
    wanted = done.get("aliases")
    models = await deps.catalog.list()
    if wanted is not None:
        models = [m for m in models if m.alias in wanted]
    return {"done": True, "models": [model_json(m) for m in models]}
