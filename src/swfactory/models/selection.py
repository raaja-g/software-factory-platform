"""Model selection: fixed pick with sticky builder model and a recorded fallback (R05, P5)."""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from swfactory.domain.types import EngineSupport, ModelRef, Role, SelectionMode
from swfactory.models.profiles import engine_support, model_family

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from swfactory.events import EventBus
    from swfactory.models.catalog import Catalog, ModelInfo
    from swfactory.models.profiles import ResolvedProfile, RoleModel


@dataclass(frozen=True, slots=True)
class SelectionContext:
    job_id: UUID
    app_slug: str
    role: Role
    round: int
    feature_chars: int
    spec_chars: int
    diff_files: int
    diff_lines: int
    prior_verdicts: Mapping[str, str]
    budget_left_usd: Decimal
    builder_family: str | None
    laya_pins: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelChoice:
    engine: str
    model: ModelRef
    mode: SelectionMode
    tier: str | None
    decision_id: UUID
    reason: str


class NoModelAvailable(Exception):
    gate_reason = "no model available"


@dataclass(frozen=True, slots=True)
class Pick:
    alias: str | None
    reason: str
    fallback_from: str | None
    options: tuple[Mapping[str, Any], ...]


def pick_model(
    rm: RoleModel,
    infos: Mapping[str, ModelInfo | None],
    *,
    sticky_alias: str | None,
    exclude: Collection[str] = (),
    support: Callable[[str, ModelInfo], EngineSupport] = engine_support,
) -> Pick:
    candidates = [rm.model, *rm.fallback]
    if rm.sticky and sticky_alias:
        candidates.insert(0, sticky_alias)
    candidates = list(dict.fromkeys(candidates))
    options: list[Mapping[str, Any]] = []
    chosen: str | None = None
    first_skipped: str | None = None
    why_skipped = ""
    for alias in candidates:
        info = infos.get(alias)
        excluded = alias in exclude
        sup = support(rm.engine, info) if info is not None else None
        options.append(
            {
                "alias": alias,
                "available": info.available if info is not None else False,
                "support": sup.value if sup is not None else None,
                "error_class": info.error_class if info is not None else None,
                "excluded": excluded,
            }
        )
        if chosen is not None:
            continue
        ok = (
            not excluded
            and info is not None
            and info.available
            and (
                sup is EngineSupport.OK
                or (sup is EngineSupport.EXPERIMENTAL and rm.allow_experimental)
            )
        )
        if ok:
            chosen = alias
            continue
        if first_skipped is None:
            first_skipped = alias
            if excluded:
                why_skipped = "failed in this step"
            elif info is None:
                why_skipped = "unavailable (unknown model)"
            elif not info.available:
                why_skipped = f"unavailable ({info.error_class or 'unknown'})"
            else:
                why_skipped = f"not supported by {rm.engine} ({sup.value if sup else 'unknown'})"
    if chosen is None:
        return Pick(None, "no model available", first_skipped, tuple(options))
    if first_skipped is None:
        reason = "sticky" if rm.sticky and sticky_alias == chosen else "primary"
        return Pick(chosen, reason, None, tuple(options))
    return Pick(chosen, f"fallback: {first_skipped} {why_skipped}", first_skipped, tuple(options))


class ModelSelector:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        catalog: Catalog,
        events: EventBus,
        *,
        support: Callable[[str, ModelInfo], EngineSupport] = engine_support,
        router: Any | None = None,
    ) -> None:
        self.sessions = sessions
        self.catalog = catalog
        self.events = events
        self.support = support
        self.router = router

    async def _router_pick(
        self, rm: RoleModel, ctx: SelectionContext, pick: Pick
    ) -> tuple[Pick, Any]:
        """Shadow/laya seam: fixed behavior in every mode until M11."""
        return pick, None

    async def choose(
        self, profile: ResolvedProfile, ctx: SelectionContext, *, exclude: Collection[str] = ()
    ) -> ModelChoice:
        from swfactory.db import repo

        t0 = time.monotonic()
        rm = profile.for_role(ctx.role)
        async with self.sessions() as s:
            sticky = await repo.last_model_decision(s, ctx.job_id, ctx.role.value)
        aliases: list[str] = [rm.model, *rm.fallback]
        if sticky:
            aliases.append(sticky)
        aliases = list(dict.fromkeys(aliases))
        infos = {a: await self.catalog.get(a) for a in aliases}
        pick = pick_model(rm, infos, sticky_alias=sticky, exclude=exclude, support=self.support)
        pick, _answer = await self._router_pick(rm, ctx, pick)
        info = infos.get(pick.alias) if pick.alias else None
        reason = pick.reason
        if (
            info is not None
            and ctx.role is Role.REVIEWER_SECURITY
            and ctx.builder_family
            and model_family(info.upstream_id) == ctx.builder_family
        ):
            reason += f"; same family as builder ({ctx.builder_family}), D17"
        async with self.sessions() as s, s.begin():
            decision_id = await repo.insert_model_decision(
                s,
                job_id=ctx.job_id,
                role=ctx.role.value,
                round=ctx.round,
                mode=rm.mode.value,
                options=list(pick.options),
                chosen=pick.alias,
                reason=reason,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        if pick.alias is None or info is None:
            raise NoModelAvailable("no model available")
        if pick.fallback_from is not None:
            await self.events.emit(
                ctx.job_id,
                "model.fallback",
                {
                    "role": ctx.role.value,
                    "round": ctx.round,
                    "requested": pick.fallback_from,
                    "used": pick.alias,
                    "source": "selection",
                },
            )
        return ModelChoice(
            engine=rm.engine,
            model=ModelRef(info.alias, info.provider, info.upstream_id),
            mode=rm.mode,
            tier=None,
            decision_id=decision_id,
            reason=reason,
        )
