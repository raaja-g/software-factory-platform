"""Metrics placeholder until scoring (M10)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from swfactory.api.auth import Principal, require

router = APIRouter()


@router.get("/metrics")
async def metrics(_: Annotated[Principal, Depends(require("viewer"))]) -> dict[str, object]:
    return {"available": False, "message": "Metrics arrive with scoring (M10)."}
