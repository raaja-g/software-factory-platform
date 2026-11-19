"""The api watchdog (§22): stuck jobs plus webhook notices, each sent once."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.db import repo
from swfactory.definition.models import Definition
from swfactory.events import EventBus
from swfactory.expiry import expiry_warning_due
from swfactory.health import detect_stuck
from swfactory.notify import Notifier, notify_once
from swfactory.sandbox.disk import disk_paths, disk_usage_pct
from swfactory.settings import Settings

log = structlog.get_logger(__name__)

NOTIFY_EVENT_KINDS = ("job.stuck", "job.failed", "job.expired", "factory.halted")
WORKERS_DOWN_AFTER = timedelta(minutes=5)
BUDGET_THRESHOLDS = (80, 100)
EVENT_WINDOW = timedelta(hours=24)

_REF = "coalesce(a.slug || ':' || lpad(j.seq::text, 3, '0'), j.slug)"


def waiting_reminder(*, waited: timedelta, after_h: int) -> int | None:
    """None before ``after_h`` hours, then 0, 1, 2 ... one more every 24 hours."""
    after = timedelta(hours=after_h)
    if waited < after:
        return None
    return (waited - after) // timedelta(hours=24)


def job_link(settings: Settings, job_id: UUID) -> str:
    base = (settings.public_url or settings.api_url).rstrip("/")
    return f"{base}/#/job/{job_id}"


def _event_text(kind: str, ref: str, data: dict[str, Any]) -> str:
    if kind == "job.failed":
        return f"{ref} failed: {data.get('error', 'unknown error')}"
    if kind == "job.stuck":
        return f"{ref} stuck: no events for {data.get('minutes', '?')} minutes"
    if kind == "job.expired":
        return f"{ref} expired after {data.get('waited_days', '?')} days waiting"
    return f"{ref}: {data.get('reason') or data.get('error') or kind}".strip(": ")


async def _fresh(s: AsyncSession) -> None:
    if s.in_transaction():
        await s.commit()


async def _events(
    s: AsyncSession, notifier: Notifier, settings: Settings, now: datetime
) -> list[str]:
    rows = (
        await s.execute(
            text(
                f"SELECT e.id, e.kind, e.data, e.job_id, {_REF} AS ref "
                "FROM app.job_events e LEFT JOIN app.jobs j ON j.id = e.job_id "
                "LEFT JOIN app.apps a ON a.id = j.app_id "
                "WHERE e.kind = ANY(:kinds) AND e.at >= :since ORDER BY e.id"
            ),
            {"kinds": list(NOTIFY_EVENT_KINDS), "since": now - EVENT_WINDOW},
        )
    ).all()
    sent: list[str] = []
    for eid, kind, data, job_id, ref in rows:
        key = f"event:{eid}"
        link = job_link(settings, job_id) if job_id else None
        body = _event_text(kind, ref or "factory", data or {})
        if await notify_once(s, notifier, key=key, kind=kind, text=body, link=link):
            sent.append(key)
    return sent


async def _waiting(
    s: AsyncSession, notifier: Notifier, defn: Definition, settings: Settings, now: datetime
) -> list[str]:
    sent: list[str] = []
    rows = (
        await s.execute(
            text(
                f"SELECT j.id, w.id, w.at, {_REF} FROM app.jobs j "
                "LEFT JOIN app.apps a ON a.id = j.app_id "
                "JOIN (SELECT DISTINCT ON (job_id) job_id, id, at FROM app.job_events "
                "      WHERE kind = 'human.waiting' ORDER BY job_id, id DESC) w ON w.job_id = j.id "
                "WHERE j.stage = 'needs_human' ORDER BY j.id"
            )
        )
    ).all()
    for job_id, eid, at, ref in rows:
        n = waiting_reminder(waited=now - at, after_h=settings.notify_waiting_after_h)
        if n is None:
            continue
        waited_h = int((now - at).total_seconds() // 3600)
        if await notify_once(
            s,
            notifier,
            key=f"waiting:{eid}:{n}",
            kind="human.waiting",
            text=f"{ref} has waited {waited_h} h for a person",
            link=job_link(settings, job_id),
        ):
            sent.append(f"waiting:{eid}:{n}")
    days = defn.factory.defaults.waiting_expiry_days
    if days > 0:
        rows = (
            await s.execute(
                text(
                    f"SELECT j.id, w.id, w.at, {_REF} FROM app.jobs j "
                    "LEFT JOIN app.apps a ON a.id = j.app_id "
                    "JOIN (SELECT DISTINCT ON (job_id) job_id, id, at FROM app.job_events "
                    "      WHERE kind IN ('job.stage', 'human.waiting') "
                    "      ORDER BY job_id, at DESC, id DESC) w ON w.job_id = j.id "
                    "WHERE j.stage IN ('needs_human', 'failed') ORDER BY j.id"
                )
            )
        ).all()
        for job_id, eid, at, ref in rows:
            if not expiry_warning_due(at, now, days):
                continue
            left = max(0, days - (now - at).days)
            key = f"expiring:{eid}"
            if await notify_once(
                s,
                notifier,
                key=key,
                kind="job.expiring",
                text=f"{ref} will be closed in about {left} days unless someone acts",
                link=job_link(settings, job_id),
            ):
                sent.append(key)
    return sent


async def _workers(s: AsyncSession, notifier: Notifier, now: datetime) -> list[str]:
    latest = await s.scalar(text("SELECT max(at) FROM app.worker_heartbeats"))
    if latest is not None and now - latest <= WORKERS_DOWN_AFTER:
        return []
    key = f"workers:{latest.isoformat() if latest else 'none'}"
    if await notify_once(
        s, notifier, key=key, kind="workers.none", text="no worker heartbeat for 5 minutes"
    ):
        return [key]
    return []


def _fmt_usd(v: Decimal) -> str:
    return format(v.normalize(), "f")


async def _budget(
    s: AsyncSession, notifier: Notifier, defn: Definition, now: datetime
) -> list[str]:
    daily = defn.factory.budgets.daily_usd
    if not daily:
        return []
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    used = await repo.spend_since(s, day, "benchmark") + await repo.open_reservations(
        s, since=day, exclude_kind="benchmark"
    )
    sent: list[str] = []
    for pct in BUDGET_THRESHOLDS:
        if used * 100 < Decimal(daily) * pct:
            continue
        key = f"budget:{day.date()}:{pct}"
        if await notify_once(
            s,
            notifier,
            key=key,
            kind="budget.daily",
            text=f"{pct}% of today's ${_fmt_usd(Decimal(daily))} budget used",
        ):
            sent.append(key)
    return sent


async def _models(s: AsyncSession, notifier: Notifier, now: datetime) -> list[str]:
    rows = (
        await s.execute(
            text(
                "SELECT alias, error_class, last_error FROM app.models "
                "WHERE available = false AND last_probe_at >= :since ORDER BY alias"
            ),
            {"since": now - EVENT_WINDOW},
        )
    ).all()
    sent: list[str] = []
    for alias, error_class, last_error in rows:
        key = f"model:{alias}:{now.astimezone(UTC).date()}"
        why = error_class or last_error or "unknown"
        if await notify_once(
            s, notifier, key=key, kind="model.unavailable", text=f"{alias} unavailable: {why}"
        ):
            sent.append(key)
    return sent


async def _disk(
    s: AsyncSession, notifier: Notifier, settings: Settings, now: datetime
) -> list[str]:
    pct, path = disk_usage_pct(disk_paths(settings))
    if pct <= settings.disk_high_water_pct:
        return []
    key = f"disk:{now.astimezone(UTC).date()}:{path}"
    if await notify_once(
        s,
        notifier,
        key=key,
        kind="disk.high",
        text=f"{path} at {pct}% (high-water {settings.disk_high_water_pct}%)",
    ):
        return [key]
    return []


async def watchdog_tick(
    s: AsyncSession,
    bus: EventBus,
    notifier: Notifier,
    *,
    defn: Definition,
    settings: Settings,
    now: datetime,
) -> list[str]:
    """One watchdog pass; returns the keys it sent. A failing check never stops the next."""
    sent: list[str] = []

    async def stuck() -> list[str]:
        await detect_stuck(s, bus, now=now)
        return []

    checks: list[tuple[str, Callable[[], Awaitable[list[str]]]]] = [
        ("stuck", stuck),
        ("events", lambda: _events(s, notifier, settings, now)),
        ("waiting", lambda: _waiting(s, notifier, defn, settings, now)),
        ("workers", lambda: _workers(s, notifier, now)),
        ("budget", lambda: _budget(s, notifier, defn, now)),
        ("models", lambda: _models(s, notifier, now)),
        ("disk", lambda: _disk(s, notifier, settings, now)),
    ]
    for name, check in checks:
        try:
            await _fresh(s)
            if name == "stuck":
                await check()
            else:
                async with s.begin():
                    sent += await check()
        except Exception as exc:
            log.warning("watchdog.failed", check=name, error=repr(exc))
            try:
                await s.rollback()
            except Exception:
                pass
    return sent
