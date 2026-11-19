"""Profile routes: list, read and save (the save goes through the definition writer)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError

from swfactory.api.auth import Principal, require
from swfactory.api.routes import definition as definition_routes
from swfactory.definition.models import (
    PROFILE_NAME_RE,
    DefinitionPolicy,
    Profile,
    validate_definition,
)
from swfactory.definition.store import NoActiveDefinition
from swfactory.definition.writer import DefinitionWriteError
from swfactory.domain.types import EngineSupport, FactoryMode
from swfactory.engines.registry import get_engine
from swfactory.models.catalog import ModelInfo
from swfactory.models.profiles import engine_support

if TYPE_CHECKING:
    from swfactory.api.app import ApiDeps

router = APIRouter()


def _support(engine: str, info: ModelInfo) -> EngineSupport:
    try:
        return get_engine(engine).support_for(info)
    except KeyError:  # engines are registered in the worker; the table is the same
        return engine_support(engine, info)


def unavailable_aliases(profile: Profile, models: Sequence[ModelInfo]) -> list[str]:
    """Aliases the profile uses that cannot run: missing, unavailable, unsupported by the
    role's engine, or experimental without allow_experimental."""
    by_alias = {m.alias: m for m in models}
    out: list[str] = []
    for rm in profile.roles.values():
        for alias in dict.fromkeys([rm.model, *rm.fallback, *(t.model for t in rm.tiers)]):
            info = by_alias.get(alias)
            bad = info is None or not info.available
            if not bad:
                assert info is not None
                support = _support(rm.engine, info)
                bad = support is EngineSupport.NO or (
                    support is EngineSupport.EXPERIMENTAL and not rm.allow_experimental
                )
            if bad and alias not in out:
                out.append(alias)
    return out


def _check_name(name: str) -> None:
    if not PROFILE_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=422, detail="profile name must be a lowercase slug")


def _profile_json(name: str, p: Profile) -> dict[str, Any]:
    return {
        "name": name,
        "description": p.description,
        "continuity": p.continuity,
        "roles": {
            r.value: rm.model_dump(mode="json", exclude_defaults=True)
            | {"engine": rm.engine, "model": rm.model}
            for r, rm in p.roles.items()
        },
    }


@router.get("/profiles")
async def list_profiles(
    request: Request, _user: Annotated[Principal, Depends(require("viewer"))]
) -> list[dict[str, Any]]:
    deps: ApiDeps = request.app.state.deps
    _, defn = await deps.definitions.active()
    models = await deps.catalog.list()
    return [
        _profile_json(n, p) | {"unavailable": unavailable_aliases(p, models)}
        for n, p in defn.profiles.items()
    ]


@router.get("/profiles/{name}")
async def get_profile(
    name: str, request: Request, _user: Annotated[Principal, Depends(require("viewer"))]
) -> dict[str, Any]:
    _check_name(name)
    deps: ApiDeps = request.app.state.deps
    _, defn = await deps.definitions.active()
    p = defn.profiles.get(name)
    if p is None:
        raise HTTPException(status_code=404, detail="no such profile")
    models = await deps.catalog.list()
    raw = p.model_dump(mode="json", exclude_defaults=True)
    return _profile_json(name, p) | {
        "yaml": yaml.safe_dump(raw, sort_keys=False),
        "unavailable": unavailable_aliases(p, models),
    }


class SaveProfile(BaseModel):
    yaml: str
    message: str = ""


@router.put("/profiles/{name}")
async def put_profile(
    name: str,
    body: SaveProfile,
    request: Request,
    principal: Annotated[Principal, Depends(require("admin"))],
) -> dict[str, Any]:
    _check_name(name)
    deps: ApiDeps = request.app.state.deps
    if deps.writer is None:
        raise HTTPException(status_code=501, detail="no definition writer is configured")
    try:
        raw = yaml.safe_load(body.yaml)
        if not isinstance(raw, dict):
            raise ValueError("a profile is a mapping")
        profile = Profile.model_validate(raw)
    except (yaml.YAMLError, ValueError, ValidationError) as e:
        raise HTTPException(status_code=422, detail={"errors": [str(e)[:500]]}) from None
    bad = unavailable_aliases(profile, await deps.catalog.list())
    if bad:
        raise HTTPException(status_code=422, detail={"unavailable": bad})
    try:
        _, defn = await deps.definitions.active()
    except NoActiveDefinition:
        raise HTTPException(status_code=409, detail="no active definition") from None
    data = defn.model_dump(mode="json", by_alias=True)
    data["profiles"][name] = profile.model_dump(mode="json", by_alias=True)
    settings = deps.settings
    policy = replace(
        DefinitionPolicy.from_settings(settings),
        require_image_digest=settings.mode is FactoryMode.SERVER,
    )
    _, issues = validate_definition(data, policy)
    if issues:
        errors = [f"{'.'.join(str(x) for x in i.loc)}: {i.msg}" for i in issues]
        raise HTTPException(status_code=422, detail={"errors": errors})
    message = body.message.strip() or f"Edit profile {name}"
    try:
        saved = await deps.writer.save_file(
            f"profiles/{name}.yaml", body.yaml, message=message, author=principal.email
        )
    except DefinitionWriteError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    out: dict[str, Any] = {
        "mode": getattr(saved, "mode", None),
        "sha": getattr(saved, "sha", None),
        "pr_url": getattr(saved, "pr_url", None),
        "active_sha": None,
    }
    if settings.mode is FactoryMode.PERSONAL:
        try:
            out["active_sha"], _ = await definition_routes.sync_definition(deps)
        except definition_routes.SyncError as e:
            raise HTTPException(status_code=422, detail={"errors": e.errors}) from None
    return out
