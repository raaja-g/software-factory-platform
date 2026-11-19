"""Webhook notifications (§22): one text message per notice, never raising."""

from __future__ import annotations

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.logging import register_secret
from swfactory.settings import Settings

log = structlog.get_logger(__name__)

SEND_TIMEOUT_S = 10


class Notifier:
    def __init__(
        self, webhook_url: str | None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = webhook_url or None
        self._transport = transport
        if self._url:
            register_secret(self._url)

    @classmethod
    def from_settings(cls, settings: Settings) -> Notifier:
        url = settings.notify_webhook_url
        return cls(url.get_secret_value() if url else None)

    async def send(self, kind: str, text: str, *, link: str | None = None) -> None:
        body = f"[factory] {kind}: {text}" + (f"\n{link}" if link else "")
        if self._url is None:
            log.info("notify.log_only", kind=kind, text=text, link=link)
            return
        try:
            async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S, transport=self._transport) as c:
                r = await c.post(self._url, json={"text": body})
                r.raise_for_status()
        except Exception as exc:
            log.warning("notify.failed", kind=kind, error=type(exc).__name__)


async def notify_once(
    s: AsyncSession,
    notifier: Notifier,
    *,
    key: str,
    kind: str,
    text: str,
    link: str | None = None,
) -> bool:
    """Send only when ``key`` has not been recorded yet; runs in the caller's transaction."""
    row = await s.execute(_INSERT, {"k": key, "kind": kind})
    if row.first() is None:
        return False
    await notifier.send(kind, text, link=link)
    return True


_INSERT = text(
    "INSERT INTO app.notifications (key, kind) VALUES (:k, :kind) "
    "ON CONFLICT (key) DO NOTHING RETURNING key"
)
