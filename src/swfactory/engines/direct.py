"""Direct engine: one chat call through the gateway, no tools, no sandbox process."""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Any, cast

import anthropic
import httpx
import langchain_core
import openai
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from swfactory.domain.types import AgentResult, AgentTask, RunStatus, Usage
from swfactory.engines.base import TableSupport, classify_failure, error_class_of, scrub
from swfactory.models.profiles import model_family
from swfactory.sandbox.base import Sandbox

if TYPE_CHECKING:
    from swfactory.settings import Settings

DIRECT_MAX_OUTPUT_TOKENS = 8192

_TIMEOUTS = (
    httpx.TimeoutException,
    openai.APITimeoutError,
    anthropic.APITimeoutError,
    asyncio.TimeoutError,
)
_CONNECTION = (httpx.ConnectError, openai.APIConnectionError, anthropic.APIConnectionError)


def build_chat_model(task: AgentTask, gateway_url: str) -> BaseChatModel:
    common: dict[str, Any] = {
        "api_key": task.gateway_key,
        "timeout": task.timeout_s,
        "max_tokens": DIRECT_MAX_OUTPUT_TOKENS,
        "max_retries": 0,
        "default_headers": {"x-litellm-tags": ",".join(f"{k}:{v}" for k, v in task.tags.items())},
    }
    base = gateway_url.rstrip("/")
    if model_family(task.model.upstream_id) == "claude":
        return init_chat_model(
            task.model.alias, model_provider="anthropic", base_url=base, **common
        )
    return init_chat_model(
        task.model.alias, model_provider="openai", base_url=f"{base}/v1", **common
    )


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


class DirectEngine(TableSupport):
    name = "direct"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, task: AgentTask, box: Sandbox | None) -> AgentResult:
        version = f"direct/langchain-core-{langchain_core.__version__}"
        missing: list[str] = []
        try:
            blocks: list[dict[str, Any]] = [{"type": "text", "text": task.prompt}]
            for path in task.attachments:
                data = await box.read_bytes(path) if box is not None else None
                if data is None:
                    missing.append(path)
                    blocks.append({"type": "text", "text": f"[missing screenshot: {path}]"})
                else:
                    blocks.append(
                        {
                            "type": "image",
                            "base64": base64.b64encode(data).decode(),
                            "mime_type": "image/png",
                        }
                    )
            messages: list[BaseMessage] = [
                SystemMessage(content=task.system_prompt),
                HumanMessage(content=cast(Any, blocks)),
            ]
            model = build_chat_model(task, self.settings.gateway_url)
            reply = await model.ainvoke(messages)
        except Exception as exc:  # adapters never raise for gateway failures
            return self._failure(task, exc, version, missing)

        meta = reply.response_metadata or {}
        used = meta.get("model_name") or meta.get("model")
        um = reply.usage_metadata or {}
        usage = Usage(
            input_tokens=int(um.get("input_tokens", 0)),
            output_tokens=int(um.get("output_tokens", 0)),
            cache_read_tokens=int((um.get("input_token_details") or {}).get("cache_read", 0)),
        )
        out_meta: dict[str, Any] = {}
        if missing:
            out_meta["missing_attachments"] = missing
        text = _text_of(reply.content)
        truncated = meta.get("finish_reason") == "length" or meta.get("stop_reason") == "max_tokens"
        if truncated:
            out_meta["reason"] = "truncated"
        return AgentResult(
            status=RunStatus.FAILED if truncated else RunStatus.OK,
            output=None if truncated else text,
            session_id=None,
            usage=usage,
            used_models=(str(used),) if used else (),
            reported_cost_usd=None,
            transcript=None,
            error="reply cut at the output limit" if truncated else None,
            engine_version=version,
            meta=out_meta,
        )

    def _failure(
        self, task: AgentTask, exc: Exception, version: str, missing: list[str]
    ) -> AgentResult:
        meta: dict[str, Any] = {}
        if missing:
            meta["missing_attachments"] = missing
        text = scrub(str(exc) or type(exc).__name__, task)
        if isinstance(exc, _TIMEOUTS):
            status = RunStatus.TIMEOUT
        else:
            status = classify_failure(text)
            if isinstance(exc, _CONNECTION):
                cls: str | None = "gateway"
            else:
                cls = error_class_of(text, status=getattr(exc, "status_code", None))
            meta["error_class"] = cls
        return AgentResult(
            status=status,
            output=None,
            session_id=None,
            usage=Usage(),
            used_models=(),
            reported_cost_usd=None,
            transcript=None,
            error=text,
            engine_version=version,
            meta=meta,
        )
