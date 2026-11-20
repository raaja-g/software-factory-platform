"""Model selection: fixed pick with sticky builder model and a recorded fallback (R05, P5)."""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from swfactory.costs.pricing import estimate_cost
from swfactory.domain.types import EngineSupport, ModelRef, Role, SelectionMode, Usage
from swfactory.models.profiles import engine_support, model_family

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from swfactory.events import EventBus
    from swfactory.models.catalog import Catalog, ModelInfo
    from swfactory.models.profiles import ResolvedProfile, RoleModel, TierOption
    from swfactory.models.router_client import LayaAnswer


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


TYPICAL_USAGE: Mapping[Role, Usage] = {
    Role.BUILDER: Usage(400_000, 40_000),
    Role.SPEC_WRITER: Usage(100_000, 10_000),
    Role.REVIEWER_SECURITY: Usage(150_000, 8_000),
    Role.REVIEWER_UX: Usage(150_000, 8_000),
    Role.REVIEWER_UI: Usage(150_000, 8_000),
    Role.REVIEWER_CODE: Usage(150_000, 8_000),
    Role.APPROVER: Usage(30_000, 2_000),
    Role.JUDGE: Usage(30_000, 2_000),
}
_DEFAULT_USAGE = Usage(100_000, 10_000)


def tier_question(role: Role, tiers: Sequence[TierOption]) -> dict[str, dict[str, Any]]:
    """The router question for a tier choice; labels are opaque, model names are never sent."""
    return {
        f"tier.{role.value}": {
            "type": "choice",
            "instructions": "Which capability tier should do this step?",
            "criteria": {t.label: t.description for t in tiers},
        }
    }


def selection_state(ctx: SelectionContext) -> dict[str, Any]:
    """The compact state the router sees (kept far below 512 tokens)."""
    return {
        "app": ctx.app_slug,
        "role": ctx.role.value,
        "round": ctx.round,
        "feature_chars": ctx.feature_chars,
        "spec_chars": ctx.spec_chars,
        "diff_files": ctx.diff_files,
        "diff_lines": ctx.diff_lines,
        "prior_verdicts": dict(ctx.prior_verdicts),
        "budget_left_usd": float(ctx.budget_left_usd),
    }


def gate_tier(
    rm: RoleModel,
    pick: Pick,
    answer: LayaAnswer,
    infos: Mapping[str, ModelInfo | None],
    budget_left: Decimal,
    role: Role,
    *,
    support: Callable[[str, ModelInfo], EngineSupport] = engine_support,
) -> tuple[Pick, str]:
    """Use the router's tier only if every gate passes; otherwise keep the fixed pick."""

    def fallback(why: str) -> tuple[Pick, str]:
        reason = f"laya fallback: {why}"
        if pick.reason not in ("primary", "sticky"):
            reason += f"; fixed: {pick.reason}"
        return replace(pick, reason=reason), reason

    if answer.error is not None:
        return fallback(f"router error ({answer.error})")
    if answer.head_version is None:
        return fallback("no fitted head")
    if answer.route != "auto":
        return fallback(f"route {answer.route}")
    label = answer.choice
    tier = next((t for t in rm.tiers if t.label == label), None)
    if tier is None:
        return fallback(f"tier {label} not allowed")
    info = infos.get(tier.model)
    if info is None:
        return fallback(f"tier {label} model unavailable (unknown model)")
    if not info.available:
        return fallback(f"tier {label} model unavailable ({info.error_class or 'unknown'})")
    sup = support(rm.engine, info)
    if not (
        sup is EngineSupport.OK or (sup is EngineSupport.EXPERIMENTAL and rm.allow_experimental)
    ):
        return fallback(f"tier {label} engine support {sup.value}")
    if estimate_cost(info, TYPICAL_USAGE.get(role, _DEFAULT_USAGE)) > budget_left:
        return fallback(f"tier {label} not affordable")
    return Pick(tier.model, f"laya: tier {label}", None, pick.options), f"laya: tier {label}"


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
        self,
        rm: RoleModel,
        ctx: SelectionContext,
        pick: Pick,
        infos: Mapping[str, ModelInfo | None],
    ) -> tuple[Pick, dict[str, Any] | None]:
        """Shadow/laya: ask the router for a tier. Never raises; fixed behavior on any problem."""
        if (
            rm.mode is SelectionMode.FIXED
            or pick.reason == "sticky"
            or not rm.tiers
            or pick.alias is None
        ):
            return pick, None
        if self.router is None:
            return replace(pick, reason="laya fallback: router not configured"), None
        questions = tier_question(ctx.role, rm.tiers)
        qid = f"tier.{ctx.role.value}"
        state = selection_state(ctx)
        try:
            answers = await self.router.decide(state, questions, pins=ctx.laya_pins or None)
            answer = answers[qid]
        except Exception:
            from swfactory.models.router_client import LayaAnswer

            answer = LayaAnswer(None, {}, None, 0, "bad response")
        info: dict[str, Any] = {
            "answer": {
                "choice": answer.choice,
                "probabilities": dict(answer.probabilities),
                "route": answer.route,
                "error": answer.error,
                "state": state,
            },
            "router_version": answer.router_version,
            "head_version": answer.head_version,
            "latency_ms": answer.latency_ms,
            "tier": None,
        }
        if rm.mode is SelectionMode.SHADOW:
            reason = "shadow: fixed used"
            if answer.error is not None:
                reason += f" (router error ({answer.error}))"
            if pick.reason not in ("primary", "sticky"):
                reason += f"; fixed: {pick.reason}"
            return replace(pick, reason=reason), info
        new, _reason = gate_tier(
            rm, pick, answer, infos, ctx.budget_left_usd, ctx.role, support=self.support
        )
        if new.alias is not None and new.reason.startswith("laya: tier "):
            info["tier"] = new.reason.removeprefix("laya: tier ")
        return new, info

    async def choose(
        self, profile: ResolvedProfile, ctx: SelectionContext, *, exclude: Collection[str] = ()
    ) -> ModelChoice:
        from swfactory.db import repo

        t0 = time.monotonic()
        rm = profile.for_role(ctx.role)
        async with self.sessions() as s:
            sticky = await repo.last_model_decision(s, ctx.job_id, ctx.role.value)
        aliases: list[str] = [rm.model, *rm.fallback, *(t.model for t in rm.tiers)]
        if sticky:
            aliases.append(sticky)
        aliases = list(dict.fromkeys(aliases))
        infos = {a: await self.catalog.get(a) for a in aliases}
        pick = pick_model(rm, infos, sticky_alias=sticky, exclude=exclude, support=self.support)
        pick, laya = await self._router_pick(rm, ctx, pick, infos)
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
                answer=laya["answer"] if laya else None,
                router_version=laya["router_version"] if laya else None,
                head_version=laya["head_version"] if laya else None,
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
            tier=laya["tier"] if laya else None,
            decision_id=decision_id,
            reason=reason,
        )
