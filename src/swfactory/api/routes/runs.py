"""Point-and-run: record a run request; a worker tick registers the repo and starts the job."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from swfactory.api.access import app_role
from swfactory.api.auth import ROLE_RANK, Principal, require
from swfactory.apps import RegisterError, find_registered, parse_repo
from swfactory.definition.models import PROFILE_NAME_RE
from swfactory.definition.store import NoActiveDefinition
from swfactory.runs import MAX_REQUIREMENT_BYTES, create_run_request, get_run_view

__all__ = ["router"]

router = APIRouter()


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    requirement: str
    profile: str | None = None
    ci_safe: bool = False
    budget_usd: Decimal | None = None


@router.post("/runs", status_code=202)
async def post_run(
    request: Request,
    body: RunBody,
    principal: Annotated[Principal, Depends(require("submitter"))],
) -> dict[str, str]:
    d = request.app.state.deps
    if not body.requirement.strip():
        raise HTTPException(status_code=422, detail="the requirement is empty")
    if len(body.requirement.encode("utf-8")) > MAX_REQUIREMENT_BYTES:
        raise HTTPException(
            status_code=413, detail=f"the requirement is over {MAX_REQUIREMENT_BYTES} bytes"
        )
    if body.profile is not None and not PROFILE_NAME_RE.fullmatch(body.profile):
        raise HTTPException(status_code=422, detail=f"'{body.profile}' is not a profile name")
    try:
        ref = parse_repo(body.repo, host_home=request.app.state.settings.host_home)
    except RegisterError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    try:
        _, defn = await d.definitions.active()
    except NoActiveDefinition:
        raise HTTPException(status_code=503, detail="no active definition") from None
    if body.budget_usd is not None:
        limit = defn.factory.budgets.job_max_usd
        if not body.budget_usd.is_finite() or body.budget_usd <= 0 or body.budget_usd > limit:
            raise HTTPException(
                status_code=422, detail=f"the budget must be above 0 and at most {limit} USD"
            )
    found = find_registered(defn, ref)
    if found is None:
        if ROLE_RANK[principal.role] < ROLE_RANK["admin"]:
            raise HTTPException(status_code=403, detail="registering a repo needs the admin role")
    else:
        slug, app_def = found
        role = app_role(
            principal.role,
            principal.email,
            getattr(app_def, "access", None),
            mode=request.app.state.mode,
        )
        if role is None:
            raise HTTPException(status_code=404, detail="app not found")
        if ROLE_RANK[role] < ROLE_RANK["submitter"]:
            raise HTTPException(status_code=403, detail=f"submitter role required for app '{slug}'")
    stored = ref.local_path if ref.kind == "local" else ref.full_name
    try:
        async with d.sessions.begin() as s:
            rid = await create_run_request(
                s,
                repo=stored or body.repo,
                requirement=body.requirement,
                profile=body.profile,
                ci_safe=body.ci_safe,
                budget_usd=body.budget_usd,
                requested_by=principal.email,
            )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"request_id": str(rid)}


@router.get("/runs/{request_id}")
async def get_run(
    request: Request,
    request_id: str,
    principal: Annotated[Principal, Depends(require("viewer"))],
) -> dict[str, Any]:
    try:
        key = UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="run request not found") from None
    async with request.app.state.deps.sessions() as s:
        view = await get_run_view(s, key)
    if view is None or (
        view["requested_by"] != principal.email and ROLE_RANK[principal.role] < ROLE_RANK["admin"]
    ):
        raise HTTPException(status_code=404, detail="run request not found")
    view.pop("requested_by")
    return view
