"""Metrics over scored jobs in the apps the caller can see."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from swfactory.api.access import visible_apps
from swfactory.api.auth import Principal, require
from swfactory.db.repo import metrics_rows
from swfactory.definition.store import NoActiveDefinition
from swfactory.scoring.metrics import summarize_metrics

router = APIRouter()


@router.get("/metrics")
async def metrics(
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
    app: str | None = None,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    deps = request.app.state.deps
    visible = None
    try:
        _, defn = await deps.definitions.active()
        visible = visible_apps(defn, principal, mode=request.app.state.mode)
    except NoActiveDefinition:
        pass
    if app is not None and visible is not None and app not in visible:
        raise HTTPException(status_code=404, detail=f"no app {app!r}")
    since = datetime.now(UTC) - timedelta(days=days)
    async with deps.sessions() as s:
        rows = await metrics_rows(s, app_slug=app, since=since, app_slugs=visible)
    return summarize_metrics(rows, window_days=days)
