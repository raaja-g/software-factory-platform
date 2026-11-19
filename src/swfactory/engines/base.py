"""The Engine protocol (spine) and the helpers every engine adapter shares."""

import json
import re
from collections.abc import Collection, Sequence
from typing import Any, Protocol

from swfactory.domain.types import (
    AgentResult,
    AgentTask,
    EngineSupport,
    ModelRef,
    RunStatus,
    ToolPolicy,
)
from swfactory.logging import redact
from swfactory.models.profiles import engine_support
from swfactory.sandbox.base import Sandbox

# ModelInfo (swfactory.models.catalog) is typed loosely here to keep this module import-light.
ModelInfo = Any

ENGINE_VERSIONS_PATH = "/etc/factory/engine-versions.json"
ERROR_CLASSES = ("gateway", "http_5xx", "rate_limit", "not_enabled", "auth")


class Engine(Protocol):
    name: str

    def support_for(self, model: ModelInfo) -> EngineSupport: ...

    async def run(self, task: AgentTask, box: Sandbox | None) -> AgentResult:
        """box is None only for "direct" without attachments.

        Adapters never raise for CLI or gateway failures: they return FAILED / TIMEOUT / BUDGET
        with the text in .error and its class in .meta["error_class"].
        """
        ...


class TableSupport:
    """Mixin: compatibility comes only from the section 8 table in models.profiles."""

    name: str

    def support_for(self, model: ModelInfo) -> EngineSupport:
        return engine_support(self.name, model)


def _norm(name: str) -> str:
    return name.rsplit("/", 1)[-1].split("@", 1)[0]


def fallback_detected(
    requested: ModelRef, used_models: Sequence[str], *, ignore: Collection[str] = ()
) -> bool:
    """True when the engine reports a model that is neither the alias nor the upstream id."""
    ok = {_norm(requested.alias), _norm(requested.upstream_id)}
    skip = {_norm(n) for n in ignore}
    return any(_norm(u) not in ok and _norm(u) not in skip for u in used_models)


def classify_failure(text: str) -> RunStatus:
    low = text.lower()
    if "budget has been exceeded" in low or "budget_exceeded" in low:
        return RunStatus.BUDGET
    return RunStatus.FAILED


_OMITTED = re.compile(r"^\[omitted: \d+ bytes\]$")


def json_lines(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _OMITTED.match(line):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


_NOT_ENABLED = ("publisher model", "not enabled", "permission_denied")
_RATE = ("resource_exhausted", "rate limit", "429")
_AUTH = ("invalid proxy server token", "401")
_GATEWAY = (
    "econnrefused", "connection refused", "connecterror", "name or service not known",
    "enotfound", "eai_again", "getaddrinfo", "unable to connect", "econnreset", "socket hang up",
)  # fmt: skip
_HTTP_5XX = re.compile(r"\b50\d\b|internal server error|bad gateway", re.I)


def error_class_of(text: str, *, status: int | None = None) -> str | None:
    if status is not None:
        if status == 401:
            return "auth"
        if status in (403, 404) or (status == 400 and "FAILED_PRECONDITION" in text):
            return "not_enabled"
        if status == 429:
            return "rate_limit"
        if 500 <= status < 600:
            return "http_5xx"
    low = text.lower()
    if "budget has been exceeded" in low or "budget_exceeded" in low:
        return None
    if any(m in low for m in _NOT_ENABLED):
        return "not_enabled"
    if any(m in low for m in _RATE):
        return "rate_limit"
    if any(m in low for m in _AUTH):
        return "auth"
    if _HTTP_5XX.search(text):
        return "http_5xx"
    if any(m in low for m in _GATEWAY):
        return "gateway"
    return None


async def collect_output(box: Sandbox, task: AgentTask, final_text: str | None) -> str | None:
    """Write roles: the output file's text. Every other role: the reply, and any file is deleted."""
    if task.output_path is None:
        return final_text
    if task.tool_policy is ToolPolicy.WRITE:
        text = await box.read_text(task.output_path)
        return text if text else final_text
    await box.remove(task.output_path)
    return final_text


async def engine_version(box: Sandbox, engine: str) -> str:
    raw = await box.read_text(ENGINE_VERSIONS_PATH)
    if not raw:
        return "unknown"
    try:
        data = json.loads(raw)
    except ValueError:
        return "unknown"
    value = data.get(engine) if isinstance(data, dict) else None
    return str(value) if value else "unknown"


def scrub(text: str, task: AgentTask) -> str:
    out = redact(text)
    if task.gateway_key:
        out = out.replace(task.gateway_key, "[run-key]")
    return out


def commit_env(task: AgentTask) -> dict[str, str]:
    return {
        "FACTORY_JOB": str(task.job_id),
        "FACTORY_ROLE": str(task.role),
        "FACTORY_MODEL": task.model.alias,
    }
