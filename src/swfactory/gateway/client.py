"""Async client for the LiteLLM gateway: run keys, spend, prices and model probes."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

GATEWAY_RETRY_BACKOFF_S: tuple[int, ...] = (2, 8, 30)
PROBE_429_BACKOFF_S: tuple[int, ...] = (2, 8)

_PER_MTOK = Decimal(1_000_000)

_NOT_ENABLED_MARKERS = (
    "FAILED_PRECONDITION",
    "Publisher Model",
    "PERMISSION_DENIED",
    "litellm.NotFoundError",
)
_QUOTA_MARKERS = ("RESOURCE_EXHAUSTED", "litellm.RateLimitError")
# The gateway's own per-model limit (enforce_model_rate_limits), not a provider 429.
_GATEWAY_LIMIT_MARKERS = ("model rate limit exceeded", "enforce_model_rate_limits")


class GatewayBudgetError(Exception):
    """The gateway refused a call because the key's budget is spent."""


class GatewayRateLimited(Exception):
    """The 429 is the gateway's own per-model limit, not the provider's quota."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    reachable: bool
    tool_call_ok: bool | None
    messages_tools_ok: bool | None
    latency_ms: int | None
    error_class: str | None
    error: str | None


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def is_budget_error(status: int, body: str) -> bool:
    low = body.lower()
    return status >= 400 and ("budget has been exceeded" in low or "budget_exceeded" in low)


def _is_gateway_limit(body: str) -> bool:
    low = body.lower()
    return "resource_exhausted" not in low and any(m in low for m in _GATEWAY_LIMIT_MARKERS)


def classify_error(status: int, body: str) -> str:
    if status == 400 and "FAILED_PRECONDITION" in body:
        return "not_enabled"
    if status in (403, 404):
        return "not_enabled"
    if status == 429:
        return "quota"
    if status == 401:
        return "auth"
    return "other"


def model_error_class(text: str) -> str | None:
    if is_budget_error(400, text) or _is_gateway_limit(text):
        return None
    if any(m in text for m in _NOT_ENABLED_MARKERS):
        return "not_enabled"
    if any(m in text for m in _QUOTA_MARKERS):
        return "quota"
    if "litellm.APIConnectionError" in text:
        return "other"
    return None


def _uuid_day(run_id: UUID) -> str:
    if run_id.version == 7:
        ms = int(run_id.hex[:12], 16)
        return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()
    return datetime.now(UTC).date().isoformat()


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        master_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {master_key}"},
            transport=transport,
            timeout=timeout_s,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        ok_statuses: Sequence[int] = (),
        raw_errors: bool = False,
    ) -> httpx.Response:
        """One call with retries on transport errors and 5xx.

        4xx raises HTTPStatusError (budget refusals raise GatewayBudgetError) unless the status is
        in ok_statuses or raw_errors is set; raw_errors returns the last 5xx too.
        """
        attempt = 0
        while True:
            try:
                resp = await self._http.request(
                    method, path, json=json_body, params=params, headers=headers
                )
            except httpx.TransportError:
                if attempt >= len(GATEWAY_RETRY_BACKOFF_S):
                    raise
                await _sleep(GATEWAY_RETRY_BACKOFF_S[attempt])
                attempt += 1
                continue
            if resp.status_code >= 500:
                if attempt >= len(GATEWAY_RETRY_BACKOFF_S):
                    if raw_errors:
                        return resp
                    resp.raise_for_status()
                await _sleep(GATEWAY_RETRY_BACKOFF_S[attempt])
                attempt += 1
                continue
            if resp.status_code >= 400:
                if is_budget_error(resp.status_code, resp.text):
                    raise GatewayBudgetError(resp.text)
                if resp.status_code not in ok_statuses and not raw_errors:
                    resp.raise_for_status()
            return resp

    async def mint_run_key(
        self,
        *,
        run_id: UUID,
        models: Sequence[str],
        max_budget_usd: Decimal,
        tags: Mapping[str, str],
        ttl_s: int,
    ) -> str:
        await self.revoke_run(
            run_id
        )  # check before act: an old key with this alias must not survive
        resp = await self._request(
            "POST",
            "/key/generate",
            json_body={
                "models": list(models),
                "max_budget": float(max_budget_usd),
                "duration": f"{ttl_s}s",
                "allowed_routes": ["llm_api_routes"],
                "key_alias": f"run-{run_id}",
                # `tags` on a key is Enterprise-only (403 on the open-source gateway): a run is
                # attributed through its key alias and this metadata
                "metadata": dict(tags),
            },
        )
        return str(resp.json()["key"])

    async def key_spend(self, key: str) -> Decimal:
        resp = await self._request("GET", "/key/info", params={"key": key})
        return Decimal(str(resp.json()["info"].get("spend") or 0))

    async def revoke(self, key: str) -> None:
        await self._request("POST", "/key/delete", json_body={"keys": [key]}, ok_statuses=(404,))

    async def revoke_run(self, run_id: UUID) -> None:
        await self._request(
            "POST", "/key/delete", json_body={"key_aliases": [f"run-{run_id}"]}, ok_statuses=(404,)
        )

    async def run_spend(self, run_id: UUID) -> Decimal | None:
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
        try:
            resp = await self._request(
                "GET",
                "/spend/logs",
                # without summarize=false the gateway returns daily summaries, not rows
                params={
                    "start_date": _uuid_day(run_id),
                    "end_date": tomorrow,
                    "summarize": "false",
                },
            )
            rows = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("logs") or []
        tag = f"run:{run_id}"
        alias = f"run-{run_id}"
        total = Decimal(0)
        for row in rows:
            tags = row.get("request_tags") or []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except ValueError:
                    tags = [tags]
            meta = row.get("metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except ValueError:
                    meta = None
            by_alias = isinstance(meta, dict) and meta.get("user_api_key_alias") == alias
            if tag in tags or by_alias:
                total += Decimal(str(row.get("spend") or 0))
        return total

    async def revoke_all_run_keys(self) -> int:
        aliases: list[str] = []
        page = 1
        while True:
            resp = await self._request(
                "GET", "/key/list", params={"page": page, "size": 100, "return_full_object": "true"}
            )
            data = resp.json()
            for k in data.get("keys", []):
                alias = k.get("key_alias") if isinstance(k, dict) else None
                if isinstance(alias, str) and alias.startswith("run-"):
                    aliases.append(alias)
            if page >= int(data.get("total_pages") or 1):
                break
            page += 1
        if aliases:
            await self._request(
                "POST", "/key/delete", json_body={"key_aliases": aliases}, ok_statuses=(404,)
            )
        return len(aliases)

    async def model_prices(self) -> dict[str, tuple[Decimal, Decimal, int | None]]:
        resp = await self._request("GET", "/model/info")
        out: dict[str, tuple[Decimal, Decimal, int | None]] = {}
        for item in resp.json().get("data", []):
            name = item.get("model_name")
            info = item.get("model_info") or {}
            cin, cout = info.get("input_cost_per_token"), info.get("output_cost_per_token")
            if not name or name in out or cin is None or cout is None:
                continue
            pin, pout = Decimal(str(cin)) * _PER_MTOK, Decimal(str(cout)) * _PER_MTOK
            if pin == 0 and pout == 0:
                continue
            ctx = info.get("max_input_tokens") or info.get("max_tokens")
            out[name] = (pin, pout, int(ctx) if ctx else None)
        return out

    # -- probe -------------------------------------------------------------------------------

    async def _probe_post(
        self, path: str, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
    ) -> httpx.Response:
        """Post once; a 429 is retried after PROBE_429_BACKOFF_S. The gateway's own limit raises."""
        attempt = 0
        while True:
            resp = await self._request(
                "POST", path, json_body=body, headers=headers, raw_errors=True
            )
            if resp.status_code != 429:
                return resp
            if attempt >= len(PROBE_429_BACKOFF_S):
                if _is_gateway_limit(resp.text):
                    raise GatewayRateLimited(resp.text)
                return resp
            await _sleep(PROBE_429_BACKOFF_S[attempt])
            attempt += 1

    async def probe(
        self, alias: str, *, tool_call: bool, messages_format: bool = False
    ) -> ProbeResult:
        t0 = time.monotonic()
        resp = await self._probe_post(
            "/v1/chat/completions",
            {"model": alias, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
        )
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            return ProbeResult(
                False,
                None,
                None,
                latency,
                classify_error(resp.status_code, resp.text),
                resp.text[:500],
            )
        tool_ok: bool | None = None
        msgs_ok: bool | None = None
        if tool_call:
            tool_ok = await self._probe_tool_call(alias)
        if messages_format:
            msgs_ok = await self._probe_messages(alias)
        return ProbeResult(True, tool_ok, msgs_ok, latency, None, None)

    async def _probe_tool_call(self, alias: str) -> bool | None:
        """Does the model answer a tool request with a valid tool call?

        True when it does, False when the model clearly does not, None when the probe could not
        tell (a timeout, a 5xx or a rate limit: the model may have been busy or still loading,
        and a failed probe must not be recorded as "no tool support" for good).

        Servers differ on `tool_choice`: OpenAI-style gateways take the forced function form, LM
        Studio answers 400 to it but accepts "required", and some take only "auto". Each form is
        tried in turn. The token limit leaves room for a reasoning model to think first."""
        tool = {
            "type": "function",
            "function": {
                "name": "record",
                "description": "Record a value.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            },
        }
        choices: list[Any] = [
            {"type": "function", "function": {"name": "record"}},
            "required",
            "auto",
        ]
        unknown = False
        for choice in choices:
            try:
                resp = await self._probe_post(
                    "/v1/chat/completions",
                    {
                        "model": alias,
                        "messages": [{"role": "user", "content": "Call record with value 'ok'."}],
                        "tools": [tool],
                        "tool_choice": choice,
                        "max_tokens": 512,
                    },
                )
            except httpx.HTTPError:
                unknown = True
                continue
            if resp.status_code >= 500 or resp.status_code in (408, 429):
                unknown = True
                continue
            if resp.status_code >= 400:
                continue  # this form of tool_choice (or tools at all) is refused; try the next
            try:
                call = resp.json()["choices"][0]["message"]["tool_calls"][0]["function"]
                json.loads(call["arguments"])
                if call["name"] == "record":
                    return True
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        return None if unknown else False

    async def _probe_messages(self, alias: str) -> bool:
        headers = {"anthropic-version": "2023-06-01"}
        tool = {
            "name": "record",
            "description": "Record a value.",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        }
        user = {"role": "user", "content": "Call record with value 'ok'."}
        first = await self._probe_post(
            "/v1/messages",
            {
                "model": alias,
                "max_tokens": 64,
                "messages": [user],
                "tools": [tool],
                "tool_choice": {"type": "tool", "name": "record"},
            },
            headers,
        )
        if first.status_code >= 400:
            return False
        try:
            content = first.json()["content"]
            use = next(b for b in content if b.get("type") == "tool_use")
            if use["name"] != "record":
                return False
            second = await self._probe_post(
                "/v1/messages",
                {
                    "model": alias,
                    "max_tokens": 64,
                    "messages": [
                        user,
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": use["id"],
                                    "content": "recorded",
                                }
                            ],
                        },
                    ],
                    "tools": [tool],
                },
                headers,
            )
            if second.status_code >= 400:
                return False
            return any(b.get("type") == "text" for b in second.json()["content"])
        except (KeyError, StopIteration, TypeError, ValueError):
            return False
