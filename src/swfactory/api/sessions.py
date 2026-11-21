"""Server-side dashboard sessions (personal mode).

A login makes a random secret; the browser's cookie holds it and the server keeps only its
sha256. A session counts only while it is not revoked, not expired and its binding matches the
current one, so changing the login user, the login password or the API token ends every session.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.db.tables import WebSession
from swfactory.settings import Settings

LAST_SEEN_EVERY = timedelta(minutes=1)
PURGE_EVERY = timedelta(hours=1)
PURGE_AFTER = timedelta(days=7)  # rows expired or revoked for longer than this are deleted

Now = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def session_binding(settings: Settings, token: str) -> str:
    """HMAC (keyed by the API token) of the login user and password the session was made under."""
    user = (settings.login_user or settings.local_user).lower()
    password = settings.login_password.get_secret_value() if settings.login_password else ""
    return hmac.new(token.encode(), f"{user}\x00{password}".encode(), hashlib.sha256).hexdigest()


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class WebSessionStore(Protocol):
    async def create(self, binding: str, ttl: timedelta) -> str:
        """Start a session and return its secret (the only copy; the store keeps a hash)."""
        ...

    async def valid(self, secret: str, binding: str) -> bool: ...

    async def revoke(self, secret: str) -> None: ...


@dataclass
class SessionRow:
    binding: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_seen_at: datetime | None = None


def _usable(
    *, binding: str, wanted: str, expires_at: datetime, revoked_at: datetime | None, now: datetime
) -> bool:
    return (
        revoked_at is None
        and expires_at > now
        and hmac.compare_digest(binding.encode(), wanted.encode())
    )


def _seen_due(last_seen: datetime | None, now: datetime) -> bool:
    return last_seen is None or now - last_seen >= LAST_SEEN_EVERY


class _Purger:
    """Runs a cleanup at most once an hour, from whichever request comes first."""

    def __init__(self, now: Now) -> None:
        self._now = now
        self._last: datetime | None = None

    def due(self) -> datetime | None:
        """The cut-off time for rows to delete, or None when a purge ran within the hour."""
        now = self._now()
        if self._last is not None and now - self._last < PURGE_EVERY:
            return None
        self._last = now
        return now - PURGE_AFTER


class InMemoryWebSessionStore:
    """For unit tests and for an app built without a database."""

    def __init__(self, now: Now = _utcnow) -> None:
        self._now = now
        self.rows: dict[str, SessionRow] = {}  # sha256(secret) -> row
        self._purger = _Purger(now)

    def _purge(self) -> None:
        cutoff = self._purger.due()
        if cutoff is None:
            return
        self.rows = {
            h: r
            for h, r in self.rows.items()
            if r.expires_at >= cutoff and (r.revoked_at is None or r.revoked_at >= cutoff)
        }

    async def create(self, binding: str, ttl: timedelta) -> str:
        self._purge()
        now = self._now()
        secret = secrets.token_urlsafe(32)
        self.rows[hash_secret(secret)] = SessionRow(binding, now, now + ttl)
        return secret

    async def valid(self, secret: str, binding: str) -> bool:
        self._purge()
        row = self.rows.get(hash_secret(secret))
        now = self._now()
        if row is None or not _usable(
            binding=row.binding,
            wanted=binding,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            now=now,
        ):
            return False
        if _seen_due(row.last_seen_at, now):
            row.last_seen_at = now
        return True

    async def revoke(self, secret: str) -> None:
        row = self.rows.get(hash_secret(secret))
        if row is not None and row.revoked_at is None:
            row.revoked_at = self._now()


class SqlWebSessionStore:
    """Sessions in ``app.web_sessions``; only sha256(secret) is stored."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], now: Now = _utcnow) -> None:
        self._sessions = sessions
        self._now = now
        self._purger = _Purger(now)

    async def _purge(self, s: AsyncSession) -> None:
        cutoff = self._purger.due()
        if cutoff is None:
            return
        await s.execute(
            delete(WebSession).where(
                or_(WebSession.expires_at < cutoff, WebSession.revoked_at < cutoff)
            )
        )

    async def create(self, binding: str, ttl: timedelta) -> str:
        now = self._now()
        secret = secrets.token_urlsafe(32)
        async with self._sessions() as s, s.begin():
            await self._purge(s)
            s.add(
                WebSession(
                    secret_hash=hash_secret(secret),
                    binding=binding,
                    created_at=now,
                    expires_at=now + ttl,
                )
            )
        return secret

    async def valid(self, secret: str, binding: str) -> bool:
        now = self._now()
        async with self._sessions() as s, s.begin():
            await self._purge(s)
            found = await s.execute(
                select(WebSession).where(WebSession.secret_hash == hash_secret(secret))
            )
            row = found.scalar_one_or_none()
            if row is None or not _usable(
                binding=row.binding,
                wanted=binding,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                now=now,
            ):
                return False
            if _seen_due(row.last_seen_at, now):
                await s.execute(
                    update(WebSession).where(WebSession.id == row.id).values(last_seen_at=now)
                )
            return True

    async def revoke(self, secret: str) -> None:
        async with self._sessions() as s, s.begin():
            await s.execute(
                update(WebSession)
                .where(
                    WebSession.secret_hash == hash_secret(secret),
                    WebSession.revoked_at.is_(None),
                )
                .values(revoked_at=self._now())
            )
