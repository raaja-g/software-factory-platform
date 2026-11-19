import base64
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from swfactory.domain.types import ModelRef, RunStatus, ToolPolicy
from swfactory.engines.base import fallback_detected
from swfactory.engines.direct import DIRECT_MAX_OUTPUT_TOKENS, DirectEngine
from swfactory.gateway.client import model_error_class
from swfactory.graph.steps import is_transient

FIXTURES = Path(__file__).parent / "fixtures"
MISSING = "/work/.factory/shots/missing.png"
SHOT = "/work/.factory/shots/shot.png"
BASE = "http://127.0.0.1:4000"

pytestmark = pytest.mark.usefixtures("respx_mock")


@pytest.fixture(autouse=True)
def _plain_http_client(monkeypatch):
    # langchain-openai builds its own socket-option transport, which respx cannot intercept
    monkeypatch.setenv("LANGCHAIN_OPENAI_TCP_KEEPALIVE", "0")


def load(name):
    return json.loads((FIXTURES / "direct" / name).read_text())


@pytest.fixture
def gemini_task(task):
    return replace(
        task,
        engine="direct",
        model=ModelRef("gemini-flash@vertex", "vertex", "gemini-2.5-flash"),
        workdir=None,
        output_path=None,
        tool_policy=ToolPolicy.READ_ONLY,
        gateway_key="sk-run-test",
        tags={"app": "a", "job": "j1", "role": "reviewer"},
    )


@pytest.fixture
def claude_task(gemini_task):
    return replace(gemini_task, model=ModelRef("claude-haiku@vertex", "vertex", "claude-haiku-4-5"))


@pytest.fixture
def gemini_task_with_png(gemini_task):
    return replace(gemini_task, attachments=(SHOT,))


@pytest.fixture
def gemini_task_missing_png(gemini_task):
    return replace(gemini_task, attachments=(MISSING,))


async def test_openai_format_for_gemini(respx_mock, settings, gemini_task):
    route = respx_mock.post(f"{BASE}/v1/chat/completions").respond(json=load("openai_chat.json"))
    r = await DirectEngine(settings).run(gemini_task, None)
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer sk-run-test"
    assert "job:" in req.headers["x-litellm-tags"]
    assert json.loads(req.content)["model"] == "gemini-flash@vertex"
    assert (
        r.status is RunStatus.OK
        and (r.output or "").startswith("VERDICT:")
        and r.usage.input_tokens > 0
    )


async def test_anthropic_format_for_claude(respx_mock, settings, claude_task):
    route = respx_mock.post(f"{BASE}/v1/messages").respond(json=load("anthropic_messages.json"))
    r = await DirectEngine(settings).run(claude_task, None)
    req = route.calls[0].request
    assert json.loads(req.content)["model"] == "claude-haiku@vertex"
    assert req.headers["x-api-key"] == "sk-run-test"
    assert r.status is RunStatus.OK and r.used_models == ("claude-haiku@vertex",)
    assert r.usage.cache_read_tokens == 40


async def test_images_from_attachments(respx_mock, settings, stub_box, gemini_task_with_png):
    data = (FIXTURES / "direct/shot.png").read_bytes()
    await stub_box.write_bytes(SHOT, data)
    route = respx_mock.post(f"{BASE}/v1/chat/completions").respond(json=load("openai_chat.json"))
    await DirectEngine(settings).run(gemini_task_with_png, stub_box)
    parts = json.loads(route.calls[0].request.content)["messages"][-1]["content"]
    img = [p for p in parts if p["type"] == "image_url"][0]["image_url"]["url"]
    assert img == "data:image/png;base64," + base64.b64encode(data).decode()


async def test_missing_attachment_noted(respx_mock, settings, stub_box, gemini_task_missing_png):
    route = respx_mock.post(f"{BASE}/v1/chat/completions").respond(json=load("openai_chat.json"))
    r = await DirectEngine(settings).run(gemini_task_missing_png, stub_box)
    assert r.meta["missing_attachments"] == [MISSING]
    assert f"[missing screenshot: {MISSING}]" in route.calls[0].request.content.decode()


async def test_swapped_model_reported(respx_mock, settings, gemini_task):
    respx_mock.post(f"{BASE}/v1/chat/completions").respond(json=load("openai_swapped.json"))
    r = await DirectEngine(settings).run(gemini_task, None)
    assert r.used_models == ("gemini-3.5-flash-lite",)
    assert fallback_detected(gemini_task.model, r.used_models) is True


async def test_budget_error_maps_to_budget(respx_mock, settings, gemini_task):
    respx_mock.post(f"{BASE}/v1/chat/completions").respond(400, json=load("budget_error.json"))
    assert (await DirectEngine(settings).run(gemini_task, None)).status is RunStatus.BUDGET


async def test_client_timeout_is_timeout(respx_mock, settings, gemini_task):
    respx_mock.post(f"{BASE}/v1/chat/completions").mock(side_effect=httpx.ReadTimeout("timed out"))
    r = await DirectEngine(settings).run(gemini_task, None)
    assert r.status is RunStatus.TIMEOUT and is_transient(r)


async def test_gateway_error_text_and_class(respx_mock, settings, gemini_task):
    respx_mock.post(f"{BASE}/v1/chat/completions").respond(
        503, json={"error": {"message": "upstream overloaded"}}
    )
    r = await DirectEngine(settings).run(gemini_task, None)
    assert (r.status, r.meta["error_class"]) == (RunStatus.FAILED, "http_5xx")
    error = r.error or ""
    assert "upstream overloaded" in error and model_error_class(error) is None


async def test_connection_error_is_gateway(respx_mock, settings, gemini_task):
    respx_mock.post(f"{BASE}/v1/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    r = await DirectEngine(settings).run(gemini_task, None)
    assert r.status is RunStatus.FAILED and r.meta["error_class"] == "gateway"


@pytest.mark.parametrize("policy", list(ToolPolicy))
async def test_tool_policy_ignored(respx_mock, settings, gemini_task, policy):
    route = respx_mock.post(f"{BASE}/v1/chat/completions").respond(json=load("openai_chat.json"))
    r = await DirectEngine(settings).run(replace(gemini_task, tool_policy=policy), None)
    body = json.loads(route.calls[0].request.content)
    assert r.status is RunStatus.OK and "tools" not in body


async def test_explicit_output_limit(respx_mock, settings, gemini_task, claude_task):
    o = respx_mock.post(f"{BASE}/v1/chat/completions").respond(json=load("openai_chat.json"))
    m = respx_mock.post(f"{BASE}/v1/messages").respond(json=load("anthropic_messages.json"))
    await DirectEngine(settings).run(gemini_task, None)
    await DirectEngine(settings).run(claude_task, None)
    ob, mb = json.loads(o.calls[0].request.content), json.loads(m.calls[0].request.content)
    assert ob.get("max_completion_tokens", ob.get("max_tokens")) == mb["max_tokens"]
    assert mb["max_tokens"] == DIRECT_MAX_OUTPUT_TOKENS == 8192


@pytest.mark.parametrize(
    "path,fixture,claude",
    [
        ("/v1/chat/completions", "openai_length.json", False),
        ("/v1/messages", "anthropic_max_tokens.json", True),
    ],
)
async def test_cut_off_reply_fails(
    respx_mock, settings, gemini_task, claude_task, path, fixture, claude
):
    respx_mock.post(f"{BASE}{path}").respond(json=load(fixture))
    r = await DirectEngine(settings).run(claude_task if claude else gemini_task, None)
    assert (r.status, r.meta["reason"], r.output) == (RunStatus.FAILED, "truncated", None)
