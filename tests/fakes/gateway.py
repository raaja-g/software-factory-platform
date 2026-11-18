"""An in-memory GatewayClient stand-in with scripted probes, spend and prices."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from uuid import UUID

from swfactory.gateway.client import ProbeResult


class FakeGateway:
    base_url = "http://fake-gateway"

    def __init__(self) -> None:
        self.probes: dict[str, ProbeResult | Exception] = {}
        self.spend: dict[str, Decimal] = {}
        self.context_windows: dict[str, int] = {}
        self.mint_error: Exception | None = None
        self.revoke_error: Exception | None = None
        self.probe_error: Exception | None = None
        self.probe_calls: dict[str, dict[str, bool]] = {}
        self.run_spends: dict[UUID, Decimal | None] = {}
        self.minted: list[dict] = []
        self.revoked: list[str] = []
        self.revoked_aliases: list[str] = []
        self.run_spend_calls: list[UUID] = []
        self.revoke_all_calls: int = 0
        self.prices: dict[str, tuple[Decimal, Decimal, int | None]] = {}

    async def mint_run_key(
        self,
        *,
        run_id: UUID,
        models: Sequence[str],
        max_budget_usd: Decimal,
        tags: Mapping[str, str],
        ttl_s: int,
    ) -> str:
        if self.mint_error is not None:
            raise self.mint_error
        key = f"sk-fake-{run_id}"
        self.minted.append(
            {
                "run_id": run_id,
                "models": list(models),
                "max_budget_usd": max_budget_usd,
                "tags": dict(tags),
                "ttl_s": ttl_s,
                "key": key,
            }
        )
        return key

    async def key_spend(self, key: str) -> Decimal:
        return self.spend.get(key, Decimal(0))

    async def revoke(self, key: str) -> None:
        if self.revoke_error is not None:
            raise self.revoke_error
        self.revoked.append(key)

    async def revoke_run(self, run_id: UUID) -> None:
        if self.revoke_error is not None:
            raise self.revoke_error
        self.revoked_aliases.append(f"run-{run_id}")

    async def run_spend(self, run_id: UUID) -> Decimal | None:
        self.run_spend_calls.append(run_id)
        return self.run_spends.get(run_id, Decimal(0))

    async def revoke_all_run_keys(self) -> int:
        self.revoke_all_calls += 1
        return 0

    async def probe(
        self, alias: str, *, tool_call: bool, messages_format: bool = False
    ) -> ProbeResult:
        self.probe_calls[alias] = {"tool_call": tool_call, "messages_format": messages_format}
        if self.probe_error is not None:
            raise self.probe_error
        scripted = self.probes.get(alias)
        if isinstance(scripted, Exception):
            raise scripted
        if scripted is not None:
            return scripted
        return ProbeResult(
            True, True if tool_call else None, True if messages_format else None, 10, None, None
        )

    async def model_prices(self) -> dict[str, tuple[Decimal, Decimal, int | None]]:
        out = dict(self.prices)
        for alias, ctx in self.context_windows.items():
            lo, hi, _ = out.get(alias, (Decimal(0), Decimal(0), None))
            out[alias] = (lo, hi, ctx)
        return out
