"""Model catalog: ModelInfo, the probe schedule and Catalog (refresh, get, list)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from swfactory.definition.models import Definition, ModelAliasDef
    from swfactory.definition.store import DefinitionStore
    from swfactory.gateway.client import GatewayClient, ProbeResult

log = structlog.get_logger()

NIGHTLY = timedelta(hours=24)
HOURLY = timedelta(hours=1)
QUOTA_RECHECK = timedelta(minutes=5)
MAX_PROBES = 4
UNPRICED_ERROR = "no price: set price_in_per_mtok and price_out_per_mtok"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    alias: str
    provider: str
    upstream_id: str
    strength: str  # strong | mid | cheap | local
    price_in_per_mtok: Decimal
    price_out_per_mtok: Decimal
    context_window: int | None
    supports_tools: bool | None
    messages_tools_ok: bool | None
    available: bool
    error_class: str | None  # not_enabled | quota | auth | other | unpriced
    latency_ms: int | None
    last_error: str | None


def _profile_aliases(rm: object) -> list[str]:
    model = getattr(rm, "model")  # noqa: B009
    fallback = getattr(rm, "fallback", ())
    tiers = getattr(rm, "tiers", ())
    return [model, *fallback, *(t.model for t in tiers)]


def aliases_in_use(defn: Definition) -> set[str]:
    out: set[str] = set()
    for p in defn.profiles.values():
        for rm in p.roles.values():
            out.update(_profile_aliases(rm))
    out.update(getattr(defn.factory.defaults, "claude_classifier_fallbacks", ()))
    return out


def messages_aliases(defn: Definition) -> set[str]:
    out: set[str] = set()
    for p in defn.profiles.values():
        for rm in p.roles.values():
            if rm.engine == "claude_code":
                out.update(_profile_aliases(rm))
    return out


def refresh_due(
    last_probe: Mapping[str, datetime | None],
    in_use: set[str],
    now: datetime,
    *,
    quota: Collection[str] = (),
) -> list[str] | None:
    """Aliases to probe now: all when any is unprobed or 24 h old, else the stale in-use ones
    (hourly) and quota-marked ones (QUOTA_RECHECK). None when nothing is due."""
    if any(t is None or now - t > NIGHTLY for t in last_probe.values()):
        return sorted(last_probe)
    due = set()
    for alias, t in last_probe.items():
        assert t is not None
        age = now - t
        if (alias in in_use and age > HOURLY) or (alias in quota and age >= QUOTA_RECHECK):
            due.add(alias)
    return sorted(due) or None


def to_model_info(
    alias: str,
    mdef: ModelAliasDef,
    probe: ProbeResult,
    price: tuple[Decimal, Decimal, int | None] | None,
    *,
    local: bool = False,
) -> ModelInfo:
    pin, pout = mdef.price_in_per_mtok, mdef.price_out_per_mtok
    ctx = mdef.context_window
    if price is not None:
        pin = price[0] if pin is None else pin
        pout = price[1] if pout is None else pout
        ctx = price[2] if ctx is None else ctx
    unpriced = not local and (pin is None or pout is None)
    available = probe.reachable
    error_class: str | None = None if probe.reachable else (probe.error_class or "other")
    last_error: str | None = None if probe.reachable else probe.error
    if unpriced:
        pin = pout = Decimal(0)
        if probe.reachable:
            available, error_class, last_error = False, "unpriced", UNPRICED_ERROR
    return ModelInfo(
        alias=alias,
        provider=mdef.provider,
        upstream_id=mdef.upstream_id,
        strength=mdef.strength,
        price_in_per_mtok=pin if pin is not None else Decimal(0),
        price_out_per_mtok=pout if pout is not None else Decimal(0),
        context_window=ctx,
        supports_tools=probe.tool_call_ok,
        messages_tools_ok=probe.messages_tools_ok,
        available=available,
        error_class=error_class,
        latency_ms=probe.latency_ms,
        last_error=last_error,
    )


class Catalog:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None,
        gateway: GatewayClient | None,
        definitions: DefinitionStore | None,
    ) -> None:
        self.sessions = sessions
        self.gateway = gateway
        self.definitions = definitions

    async def refresh(self, aliases: Sequence[str] | None = None) -> list[ModelInfo]:
        if self.gateway is None:
            raise RuntimeError("catalog refresh runs in the worker")
        from swfactory.db import repo
        from swfactory.gateway.client import GatewayRateLimited
        from swfactory.gateway.render import LOCAL_KINDS

        assert self.sessions is not None and self.definitions is not None
        gateway = self.gateway
        _, defn = await self.definitions.active()
        models = defn.factory.models
        wanted = list(models) if aliases is None else [a for a in aliases if a in models]
        messages = messages_aliases(defn)
        prices = await gateway.model_prices()
        gate = asyncio.Semaphore(MAX_PROBES)

        async def one(alias: str) -> ProbeResult | None:
            async with gate:
                try:
                    return await gateway.probe(
                        alias, tool_call=True, messages_format=alias in messages
                    )
                except GatewayRateLimited:
                    log.info("catalog.probe_rate_limited", alias=alias)
                    return None

        # an unreachable gateway raises here, before any row is written
        results = await asyncio.gather(*(one(a) for a in wanted))
        infos: list[ModelInfo] = []
        for alias, probe in zip(wanted, results, strict=True):
            if probe is None:
                continue
            mdef = models[alias]
            local = defn.factory.providers[mdef.provider].kind in LOCAL_KINDS
            infos.append(to_model_info(alias, mdef, probe, prices.get(alias), local=local))
        async with self.sessions() as s, s.begin():
            await repo.upsert_models(s, infos, probed_at=datetime.now(UTC))
            if aliases is None:
                await repo.mark_models_not_in_definition(s, list(models))
        return infos

    async def get(self, alias: str) -> ModelInfo | None:
        from swfactory.db import repo

        assert self.sessions is not None
        async with self.sessions() as s:
            return await repo.get_model(s, alias)

    async def list(self, *, available_only: bool = False) -> list[ModelInfo]:
        from swfactory.db import repo

        assert self.sessions is not None
        async with self.sessions() as s:
            return await repo.list_models(s, available_only=available_only)

    async def mark_unavailable(self, alias: str, error_class: str, error: str) -> None:
        from swfactory.db import repo

        assert self.sessions is not None
        async with self.sessions() as s, s.begin():
            await repo.mark_model_unavailable(s, alias, error_class, error)

    async def refresh_if_due(self, now: datetime | None = None) -> list[ModelInfo] | None:
        """One worker tick: under an advisory lock, refresh what refresh_due says. None when the
        lock is held elsewhere or nothing is due."""
        assert self.sessions is not None
        async with self.sessions() as s, s.begin():
            if not await _try_lock(s):
                return None
            return await self._refresh_due(s, now)

    async def _refresh_due(self, s: AsyncSession, now: datetime | None) -> list[ModelInfo] | None:
        from swfactory.db import repo

        if self.gateway is None:
            raise RuntimeError("catalog refresh runs in the worker")
        assert self.definitions is not None
        now = now or datetime.now(UTC)
        _, defn = await self.definitions.active()
        state = await repo.model_probe_state(s)
        last = {a: state[a][0] if a in state else None for a in defn.factory.models}
        quota = {a for a, (_, e) in state.items() if e == "quota"}
        due = refresh_due(last, aliases_in_use(defn), now, quota=quota)
        if due is None:
            return None
        return await self.refresh(None if set(due) == set(defn.factory.models) else due)


async def _try_lock(s: AsyncSession) -> bool:
    got = await s.scalar(text("select pg_try_advisory_xact_lock(hashtext('catalog_refresh'))"))
    return bool(got)


_OPEN_REQUEST = text(
    """
    select id, data from app.job_events
    where job_id is null and kind = 'models.refresh_requested'
      and id > coalesce((select max((data->>'request_id')::bigint) from app.job_events
                         where job_id is null and kind = 'models.refreshed'), 0)
    order by id limit 1
    """
)


async def catalog_tick(
    sessions: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    *,
    now: datetime,
    before: Callable[[], Awaitable[bool]] | None = None,
) -> int | None:
    """One worker tick under the catalog advisory lock: serve the oldest open
    ``models.refresh_requested`` event (returns its id), else run the due refresh (returns None).
    A refresh that raises leaves the request open for the next tick."""
    from swfactory.events import record_event

    async with sessions() as s, s.begin():
        if not await _try_lock(s):
            return None
        if before is not None and await before():
            # the gateway restarted with a new config: probe every alias again
            await catalog.refresh(None)
        row = (await s.execute(_OPEN_REQUEST)).first()
        if row is None:
            await catalog._refresh_due(s, now)  # noqa: SLF001
            return None
        request_id = int(row[0])
        aliases = (row[1] or {}).get("aliases")
        try:
            infos = await catalog.refresh(aliases)
        except Exception as exc:
            log.warning("catalog.gateway_unreachable", request_id=request_id, error=repr(exc))
            return None
        await record_event(
            s,
            None,
            "models.refreshed",
            {
                "request_id": request_id,
                "aliases": list(aliases) if aliases is not None else [i.alias for i in infos],
                "available": sum(1 for i in infos if i.available),
            },
        )
        return request_id
