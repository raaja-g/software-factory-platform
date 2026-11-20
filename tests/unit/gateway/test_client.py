from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from swfactory.gateway import client as client_module
from swfactory.gateway.client import (
    GATEWAY_RETRY_BACKOFF_S,
    PROBE_429_BACKOFF_S,
    GatewayBudgetError,
    GatewayClient,
    GatewayRateLimited,
    classify_error,
    is_budget_error,
    model_error_class,
)

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "gateway"
RUN = uuid.UUID("0190f3a0-5c00-7000-8000-000000000001")
OTHER = uuid.UUID("0190f3a0-5c00-7000-8000-000000000002")
RUN_DAY = datetime.fromtimestamp(int(RUN.hex[:12], 16) / 1000, UTC).date().isoformat()
TAGS = {"run": str(RUN), "role": "builder"}


def fixture(name: str) -> str:
    return (FIX / name).read_text()


def record(into: list[float]):
    async def _rec(s: float) -> None:
        into.append(s)

    return _rec


class Server:
    """A scripted gateway over httpx.MockTransport that records every request."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str, Any, dict[str, str]]] = []
        self.handlers: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.log.append((request.method, request.url.path, body, dict(request.url.params)))
        h = self.handlers.get(request.url.path)
        if h is None:
            return httpx.Response(200, json={})
        if isinstance(h, list):
            h = h.pop(0) if len(h) > 1 else h[0]
        if isinstance(h, Exception):
            raise h
        if callable(h):
            return h(request)  # type: ignore[no-any-return]
        return h  # type: ignore[no-any-return]

    def calls(self, path: str) -> int:
        return sum(1 for _, p, _, _ in self.log if p == path)

    def sent(self, path: str) -> Any:
        return next(b for _, p, b, _ in self.log if p == path)

    def sent_json(self, path: str) -> list[Any]:
        return [b for _, p, b, _ in self.log if p == path]

    def sent_params(self, path: str) -> dict[str, str]:
        return next(q for _, p, _, q in self.log if p == path)

    def sent_before(self, a: str, b: str) -> bool:
        paths = [p for _, p, _, _ in self.log]
        return paths.index(a) < paths.index(b)


@pytest.fixture
def srv() -> Server:
    return Server()


@pytest.fixture
def gw(srv: Server) -> GatewayClient:
    return GatewayClient("http://gw", "sk-master", transport=httpx.MockTransport(srv))


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json=data)


def chat_ok() -> httpx.Response:
    return ok({"choices": [{"message": {"content": "x"}}]})


def chat_tool(args: str = '{"value": "x"}', name: str = "record") -> httpx.Response:
    call = {"id": "c1", "type": "function", "function": {"name": name, "arguments": args}}
    return ok({"choices": [{"message": {"content": None, "tool_calls": [call]}}]})


async def test_mint_run_key_payload(gw: GatewayClient, srv: Server):
    srv.handlers["/key/generate"] = ok({"key": "sk-run"})
    key = await gw.mint_run_key(
        run_id=RUN, models=["flash@vertex"], max_budget_usd=Decimal("2.5"), tags=TAGS, ttl_s=1500
    )
    body = srv.sent("/key/generate")
    assert key == "sk-run"
    assert (
        body["models"] == ["flash@vertex"]
        and body["max_budget"] == 2.5
        and body["duration"] == "1500s"
    )
    assert body["allowed_routes"] == ["llm_api_routes"]
    assert body["key_alias"] == f"run-{RUN}" and body["metadata"]["run"] == str(RUN)
    # `tags` on a key is Enterprise-only: the open-source gateway answers 403 to it
    assert "tags" not in body
    assert srv.sent_before("/key/delete", "/key/generate")
    assert srv.sent("/key/delete") == {"key_aliases": [f"run-{RUN}"]}


async def test_key_spend_and_revoke(gw: GatewayClient, srv: Server):
    srv.handlers["/key/info"] = ok({"info": {"spend": 0.0123}})
    assert await gw.key_spend("sk-run") == Decimal("0.0123")
    assert srv.sent_params("/key/info") == {"key": "sk-run"}
    await gw.revoke("sk-run")
    assert srv.sent("/key/delete") == {"keys": ["sk-run"]}


async def test_authorization_header():
    seen: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["authorization"])
        return ok({"info": {"spend": 0}})

    await GatewayClient("http://gw", "sk-master", transport=httpx.MockTransport(h)).key_spend("k")
    assert seen == ["Bearer sk-master"]


async def test_model_prices_skip_unknown_cost(gw: GatewayClient, srv: Server):
    def m(name: str, i: Any, o: Any, ctx: Any = None) -> dict[str, Any]:
        info: dict[str, Any] = {"input_cost_per_token": i, "output_cost_per_token": o}
        if ctx:
            info["max_input_tokens"] = ctx
        return {"model_name": name, "model_info": info}

    srv.handlers["/model/info"] = ok(
        {
            "data": [
                m("flash@vertex", 0.30e-6, 2.50e-6, 1_000_000),
                m("byo@openai", None, 0),
                m("zero@x", 0, 0),
                m("noctx@x", 1e-6, 2e-6),
            ]
        }
    )
    prices = await gw.model_prices()
    assert prices["flash@vertex"][:2] == (Decimal("0.30"), Decimal("2.50"))
    assert prices["flash@vertex"][2] == 1_000_000
    assert prices["noctx@x"][2] is None
    assert "byo@openai" not in prices and "zero@x" not in prices


async def test_revoke_run_by_alias_ignores_404(gw: GatewayClient, srv: Server):
    srv.handlers["/key/delete"] = httpx.Response(404, json={"error": "gone"})
    await gw.revoke_run(RUN)
    assert (
        srv.sent("/key/delete") == {"key_aliases": [f"run-{RUN}"]} and srv.calls("/key/delete") == 1
    )


async def test_run_spend_sums_tagged_rows(gw: GatewayClient, srv: Server):
    srv.handlers["/spend/logs"] = ok(
        [
            {"spend": 0.01, "request_tags": [f"run:{RUN}", "role:builder"]},
            {"spend": 0.02, "request_tags": [f"run:{RUN}"]},
            {"spend": 5, "request_tags": [f"run:{OTHER}"]},
            {"spend": 7, "request_tags": None},
        ]
    )
    assert await gw.run_spend(RUN) == Decimal("0.03")
    assert srv.sent_params("/spend/logs")["start_date"] == RUN_DAY
    assert srv.sent_params("/spend/logs")["summarize"] == "false"  # else daily summaries come back


async def test_run_spend_matches_rows_by_key_alias(gw: GatewayClient, srv: Server):
    # the open-source gateway cannot tag a key, so a row's run is its key alias in the row metadata
    srv.handlers["/spend/logs"] = ok(
        [
            {
                "spend": 0.01,
                "request_tags": ["User-Agent: x"],
                "metadata": {"user_api_key_alias": f"run-{RUN}"},
            },
            {"spend": 0.02, "metadata": json.dumps({"user_api_key_alias": f"run-{RUN}"})},
            {"spend": 5, "metadata": {"user_api_key_alias": f"run-{OTHER}"}},
            {"spend": 7, "metadata": {"user_api_key_alias": None}},
            {"spend": 9, "metadata": "not json"},
        ]
    )
    assert await gw.run_spend(RUN) == Decimal("0.03")


async def test_run_spend_none_when_unreadable(gw: GatewayClient, srv: Server, monkeypatch):
    monkeypatch.setattr(client_module, "_sleep", record([]))
    srv.handlers["/spend/logs"] = httpx.Response(500)
    assert await gw.run_spend(RUN) is None


async def test_revoke_all_run_keys(gw: GatewayClient, srv: Server):
    pages = {
        "1": {"keys": [{"key_alias": "run-a"}, {"key_alias": "ops-admin"}], "total_pages": 2},
        "2": {"keys": [{"key_alias": "run-b"}, {"key_alias": None}], "total_pages": 2},
    }
    srv.handlers["/key/list"] = lambda r: ok(pages[r.url.params.get("page", "1")])
    assert await gw.revoke_all_run_keys() == 2
    assert srv.sent("/key/delete") == {"key_aliases": ["run-a", "run-b"]}


@pytest.mark.parametrize(
    "status,body,cls",
    [
        (400, "FAILED_PRECONDITION", "not_enabled"),
        (403, "", "not_enabled"),
        (404, "Publisher Model was not found", "not_enabled"),
        (429, "RESOURCE_EXHAUSTED", "quota"),
        (401, "", "auth"),
        (500, "", "other"),
    ],
)
def test_classify_error(status: int, body: str, cls: str):
    assert classify_error(status, body) == cls


@pytest.mark.parametrize(
    "text,cls",
    [
        (fixture("errors/not_enabled.json"), "not_enabled"),
        (fixture("errors/quota.json"), "quota"),
        ("httpx.ConnectError: [Errno 111] Connection refused", None),
        ("connect ECONNREFUSED 172.18.0.4:4000", None),
        ("getaddrinfo ENOTFOUND gateway-edge", None),
        ("Error: 404 Not Found", None),
        ("429 Too Many Requests", None),
        ("APIConnectionError: Connection error.", None),
        (fixture("errors/upstream_down.json"), "other"),
        (fixture("errors/gateway_not_enabled.json"), "not_enabled"),
        (fixture("errors/gateway_rate_limit.json"), None),
        ("contract: no VERDICT line", None),
        (fixture("budget-exceeded.json"), None),
        ("boom", None),
    ],
)
def test_model_error_class(text: str, cls: str | None):
    assert model_error_class(text) == cls


async def test_budget_refusal_raises(gw: GatewayClient, srv: Server):
    srv.handlers["/v1/chat/completions"] = httpx.Response(400, text=fixture("budget-exceeded.json"))
    with pytest.raises(GatewayBudgetError):
        await gw.probe("flash@vertex", tool_call=False)
    assert is_budget_error(400, fixture("budget-exceeded.json"))
    assert srv.calls("/v1/chat/completions") == 1


async def test_probe_forced_tool_call(gw: GatewayClient, srv: Server):
    srv.handlers["/v1/chat/completions"] = [chat_ok(), chat_tool()]
    r = await gw.probe("flash@vertex", tool_call=True)
    assert r.reachable and r.tool_call_ok and r.latency_ms is not None
    sent = srv.sent_json("/v1/chat/completions")
    assert sent[0]["max_tokens"] == 1
    assert sent[1]["tool_choice"]["function"]["name"] == "record"


async def test_probe_bad_tool_arguments(gw: GatewayClient, srv: Server):
    srv.handlers["/v1/chat/completions"] = [chat_ok(), chat_tool("{not json")]
    r = await gw.probe("flash@vertex", tool_call=True)
    assert r.reachable and r.tool_call_ok is False


async def test_probe_messages_round_trip(gw: GatewayClient, srv: Server):
    srv.handlers["/v1/chat/completions"] = [chat_ok(), chat_tool()]
    srv.handlers["/v1/messages"] = [
        ok(
            {
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "record", "input": {"value": "x"}}
                ]
            }
        ),
        ok({"content": [{"type": "text", "text": "done"}]}),
    ]
    r = await gw.probe("sonnet@vertex", tool_call=True, messages_format=True)
    assert r.messages_tools_ok is True
    first, second = srv.sent_json("/v1/messages")
    assert first["tool_choice"] == {"type": "tool", "name": "record"}
    assert second["messages"][-1]["content"][0]["type"] == "tool_result"


async def test_probe_gateway_unreachable_raises(gw: GatewayClient, srv: Server, monkeypatch):
    monkeypatch.setattr(client_module, "_sleep", record([]))
    srv.handlers["/v1/chat/completions"] = httpx.ConnectError("down")
    with pytest.raises(httpx.ConnectError):
        await gw.probe("flash@vertex", tool_call=False)


async def test_probe_quota(gw: GatewayClient, srv: Server, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", record(slept))
    srv.handlers["/v1/chat/completions"] = httpx.Response(429, text=fixture("errors/quota.json"))
    r = await gw.probe("pro@vertex", tool_call=True)
    assert (r.reachable, r.error_class, r.tool_call_ok) == (False, "quota", None)
    assert srv.calls("/v1/chat/completions") == 3 and slept == list(PROBE_429_BACKOFF_S) == [2, 8]


async def test_probe_gateway_limit_is_not_quota(gw: GatewayClient, srv: Server, monkeypatch):
    monkeypatch.setattr(client_module, "_sleep", record([]))
    srv.handlers["/v1/chat/completions"] = httpx.Response(
        429, text=fixture("errors/gateway_rate_limit.json")
    )
    with pytest.raises(GatewayRateLimited):
        await gw.probe("pro@vertex", tool_call=False)


async def test_probe_upstream_5xx_is_other(gw: GatewayClient, srv: Server, monkeypatch):
    monkeypatch.setattr(client_module, "_sleep", record([]))
    srv.handlers["/v1/chat/completions"] = httpx.Response(
        500, text=fixture("errors/upstream_down.json")
    )
    r = await gw.probe("qwen@lmstudio", tool_call=False)
    assert (r.reachable, r.error_class) == (False, "other")


async def test_retries_transport_errors_and_5xx(gw: GatewayClient, srv: Server, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", record(slept))
    srv.handlers["/key/info"] = [
        httpx.ConnectError("x"),
        httpx.Response(502),
        ok({"info": {"spend": 0.0123}}),
    ]
    assert await gw.key_spend("sk-run") == Decimal("0.0123") and slept == [2, 8]


async def test_gives_up_after_three_retries(gw: GatewayClient, srv: Server, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", record(slept))
    srv.handlers["/key/delete"] = httpx.ConnectError("x")
    with pytest.raises(httpx.ConnectError):
        await gw.revoke("sk-run")
    assert slept == list(GATEWAY_RETRY_BACKOFF_S) == [2, 8, 30] and srv.calls("/key/delete") == 4


async def test_4xx_is_not_retried(gw: GatewayClient, srv: Server):
    srv.handlers["/key/info"] = httpx.Response(404)
    with pytest.raises(httpx.HTTPStatusError):
        await gw.key_spend("sk-gone")
    assert srv.calls("/key/info") == 1


async def test_probe_429_then_ok(gw: GatewayClient, srv: Server, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", record(slept))
    srv.handlers["/v1/chat/completions"] = [
        httpx.Response(429, text=fixture("errors/quota.json")),
        chat_ok(),
    ]
    r = await gw.probe("pro@vertex", tool_call=False)
    assert (r.reachable, r.error_class, slept) == (True, None, [2])
