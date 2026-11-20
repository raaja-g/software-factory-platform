"""Runtime profiles: resolution, engine compatibility (section 8) and validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from swfactory.domain.types import EngineSupport, Role, SelectionMode

if TYPE_CHECKING:
    from swfactory.definition.models import AppDef, Definition, Profile, RoleOverride
    from swfactory.models.catalog import ModelInfo

_OPENAI_O = re.compile(r"^o\d")


@dataclass(frozen=True, slots=True)
class TierOption:
    label: str
    model: str
    description: str


@dataclass(frozen=True, slots=True)
class RoleModel:
    engine: str
    model: str
    mode: SelectionMode
    tiers: tuple[TierOption, ...]
    fallback: tuple[str, ...]
    sticky: bool
    allow_experimental: bool


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    name: str
    roles: Mapping[Role, RoleModel]
    continuity: Literal["fresh", "resume"] = "fresh"

    def for_role(self, role: Role) -> RoleModel:
        rm = self.roles.get(role)
        if rm is not None:
            return rm
        if role is Role.REQUIREMENTS:
            b = self.roles[Role.BUILDER]
            return RoleModel(
                engine=b.engine,
                model=b.model,
                mode=SelectionMode.FIXED,
                tiers=(),
                fallback=b.fallback,
                sticky=False,
                allow_experimental=b.allow_experimental,
            )
        raise KeyError(f"profile {self.name} has no {role.value}")

    @classmethod
    def from_def(
        cls, name: str, pdef: Profile, *, default_continuity: Literal["fresh", "resume"] = "fresh"
    ) -> ResolvedProfile:
        roles: dict[Role, RoleModel] = {}
        for role, rp in pdef.roles.items():
            roles[role] = RoleModel(
                engine=rp.engine,
                model=rp.model,
                mode=rp.mode,
                tiers=tuple(TierOption(t.label, t.model, t.description) for t in rp.tiers),
                fallback=tuple(rp.fallback),
                sticky=rp.sticky if rp.sticky is not None else role is Role.BUILDER,
                allow_experimental=rp.allow_experimental,
            )
        return cls(
            name=name,
            roles=MappingProxyType(roles),
            continuity=pdef.continuity or default_continuity,
        )


def resolve_profile(
    defn: Definition,
    app: AppDef,
    *,
    override: str | None,
    triage_profile: str | None,
    role_overrides: Mapping[Role, RoleOverride] | None = None,
) -> tuple[ResolvedProfile, str]:
    """Pick the profile: override, then triage (if the app allows it), app, global.

    ``role_overrides`` (benchmark configurations) replace each set field of a role's model."""
    if override is not None:
        name, source = override, "override"
    elif triage_profile is not None and triage_profile in app.allowed_profiles:
        name, source = triage_profile, "triage"
    elif app.profile is not None:
        name, source = app.profile, "app"
    else:
        name, source = defn.factory.defaults.profile, "global"
    if name not in defn.profiles:
        raise KeyError(f"unknown profile '{name}'")
    resolved = ResolvedProfile.from_def(
        name, defn.profiles[name], default_continuity=defn.factory.defaults.builder_continuity
    )
    if role_overrides:
        roles = dict(resolved.roles)
        for role, ov in role_overrides.items():
            try:
                base = resolved.for_role(role)
            except KeyError:
                continue
            changes = {
                k: v
                for k, v in (("engine", ov.engine), ("model", ov.model), ("mode", ov.mode))
                if v is not None
            }
            roles[role] = replace(base, **changes)
        resolved = replace(resolved, roles=MappingProxyType(roles))
    return resolved, source


def model_family(upstream_id: str) -> str:
    uid = upstream_id.lower()
    if uid.startswith("gpt-") or _OPENAI_O.match(uid):
        return "openai"
    return re.split(r"[-/]", uid, maxsplit=1)[0]


def engine_support(engine: str, info: ModelInfo) -> EngineSupport:
    """The section 8 compatibility table. Availability is not part of it."""
    family = model_family(info.upstream_id)
    if engine == "claude_code":
        if info.messages_tools_ok is False:
            return EngineSupport.NO
        return EngineSupport.OK if family == "claude" else EngineSupport.EXPERIMENTAL
    if engine == "opencode":
        if info.supports_tools is True:
            return EngineSupport.OK
        if info.supports_tools is False:
            return EngineSupport.NO
        return EngineSupport.EXPERIMENTAL
    if engine == "gemini_cli":
        return EngineSupport.OK if family == "gemini" else EngineSupport.EXPERIMENTAL
    if engine in ("direct", "fake"):
        return EngineSupport.OK
    return EngineSupport.NO


def _aliases(rm: Any) -> list[str]:
    out = [rm.model, *rm.fallback, *(t.model for t in rm.tiers)]
    return list(dict.fromkeys(out))


def validate_profile(profile: Any, infos: Mapping[str, ModelInfo]) -> list[str]:
    """Errors for a profile (a definition Profile or a ResolvedProfile)."""
    errors: list[str] = []
    for role, rm in profile.roles.items():
        for alias in _aliases(rm):
            info = infos.get(alias)
            if info is None:
                errors.append(f"{role.value}: unknown model alias {alias}")
                continue
            support = engine_support(rm.engine, info)
            if support is EngineSupport.NO:
                errors.append(f"{role.value}: {rm.engine} with {alias} is not supported")
            elif support is EngineSupport.EXPERIMENTAL and not rm.allow_experimental:
                errors.append(
                    f"{role.value}: {rm.engine} with {alias} is experimental "
                    "and allow_experimental is false"
                )
    return errors


def profile_warnings(profile: Any, infos: Mapping[str, ModelInfo]) -> list[str]:
    """D17: the security reviewer should not share the builder's model family."""
    builder = profile.roles.get(Role.BUILDER)
    sec = profile.roles.get(Role.REVIEWER_SECURITY)
    if builder is None or sec is None:
        return []
    b_info, s_info = infos.get(builder.model), infos.get(sec.model)
    if b_info is None or s_info is None:
        return []
    fam = model_family(b_info.upstream_id)
    if model_family(s_info.upstream_id) != fam:
        return []
    return [
        f"{Role.REVIEWER_SECURITY.value}: {sec.model} is the builder's family ({fam}); "
        "D17 prefers another family"
    ]
