"""Laya router sidecar client (M11.2). `decide` never raises: errors become per-question answers."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from swfactory.definition.models import Definition
    from swfactory.settings import Settings

ROUTER_ERRORS = (
    "timeout",
    "unreachable",
    "unauthorized",
    "http <code>",
    "bad response",
    "missing answer",
)
BATCH_PATH = "/v1/systemone/batch"
VERSION_HEADER = "x-factory-router-version"


@dataclass(frozen=True, slots=True)
class LayaAnswer:
    choice: str | None
    probabilities: Mapping[str, float]
    route: str | None
    latency_ms: int
    error: str | None
    head_version: str | None = None
    router_version: str | None = None


class RouterClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._transport = transport

    async def decide(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Mapping[str, Any]],
        *,
        pins: Mapping[str, int] | None = None,
    ) -> dict[str, LayaAnswer]:
        started = time.monotonic()

        def failed(error: str) -> dict[str, LayaAnswer]:
            ms = int((time.monotonic() - started) * 1000)
            return {q: LayaAnswer(None, {}, None, ms, error) for q in questions}

        try:
            body: dict[str, Any] = {"states": [state], "questions": questions}
            if pins:
                body["pins"] = dict(pins)
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout_s, transport=self._transport
                ) as http:
                    resp = await http.post(
                        self._base_url + BATCH_PATH,
                        json=body,
                        headers={"authorization": f"Bearer {self._api_key}"},
                    )
            except httpx.TimeoutException:
                return failed("timeout")
            except httpx.TransportError:
                return failed("unreachable")
            if resp.status_code in (401, 403):
                return failed("unauthorized")
            if resp.status_code != 200:
                return failed(f"http {resp.status_code}")
            ms = int((time.monotonic() - started) * 1000)
            try:
                answers = resp.json()["results"][0]["answers"]
                if not isinstance(answers, dict):
                    raise TypeError("answers")
            except (ValueError, KeyError, IndexError, TypeError):
                return failed("bad response")
            version = resp.headers.get(VERSION_HEADER)
            out: dict[str, LayaAnswer] = {}
            for q in questions:
                a = answers.get(q)
                if not isinstance(a, dict):
                    out[q] = LayaAnswer(None, {}, None, ms, "missing answer", None, version)
                    continue
                hv = a.get("head_version")
                out[q] = LayaAnswer(
                    choice=a.get("choice"),
                    probabilities=dict(a.get("probabilities") or {}),
                    route=a.get("route"),
                    latency_ms=ms,
                    error=None,
                    head_version=None if hv is None else str(hv),
                    router_version=version,
                )
            return out
        except Exception:
            return failed("bad response")


def make_router_client(settings: Settings, defn: Definition) -> RouterClient | None:
    if not settings.laya_url:
        return None
    key = settings.laya_api_key.get_secret_value() if settings.laya_api_key else ""
    return RouterClient(
        settings.laya_url, key, timeout_s=float(defn.factory.defaults.router_timeout_s)
    )
