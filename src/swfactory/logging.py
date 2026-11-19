"""Structured JSON logging to stderr with secret redaction and job context."""

import logging
import re
import sys
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID

import structlog

REDACTED = "[REDACTED]"
_secrets: set[str] = set()

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization:\s*)\w+\s+\S+"), r"\1" + REDACTED),
    (re.compile(r"Bearer\s+\S+"), REDACTED),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), REDACTED),
    (re.compile(r"ya29\.[\w-]+"), REDACTED),
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"), REDACTED),
    (re.compile(r"(://[^:/@\s]+:)[^@\s]+@"), r"\1" + REDACTED + "@"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        REDACTED,
    ),
]


def register_secret(value: str) -> None:
    """Register a secret so redact() removes it; values under 8 characters are ignored."""
    if value and len(value) >= 8:
        _secrets.add(value)


def redact(text: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _walk(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {k: _walk(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_walk(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return [_walk(v) for v in value]
    if isinstance(value, (int, float, bool, type(None))):
        return value
    return redact(str(value))


def redact_processor(_logger: Any, _name: str, event_dict: Any) -> Any:
    return _walk(event_dict)


def configure_logging(level: str = "INFO") -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    pre_chain: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.format_exc_info,
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    structlog.configure(
        processors=[
            *pre_chain,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


def log_context(
    *,
    job_id: UUID | str | None = None,
    run_id: UUID | str | None = None,
    role: str | None = None,
) -> AbstractContextManager[None]:
    fields = {
        k: str(v)
        for k, v in (("job_id", job_id), ("run_id", run_id), ("role", role))
        if v is not None
    }
    return structlog.contextvars.bound_contextvars(**fields)


def safe_error(exc: BaseException) -> str:
    """An exception reduced to its type and HTTP status, for log lines.

    The message of an HTTP error can hold the request URL, and a gateway run key travels in a URL
    query, so the message is never logged."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")
