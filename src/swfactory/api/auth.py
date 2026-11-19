"""Authentication, mode safety and request guards for the API."""

import asyncio
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import structlog
from fastapi import HTTPException, Request
from google.auth import jwt as google_jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.db.tables import ApiToken, User
from swfactory.domain.types import FactoryMode
from swfactory.settings import Settings

log = structlog.get_logger(__name__)

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
GCE_METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/"
ROLE_RANK = {"viewer": 1, "submitter": 2, "approver": 3, "admin": 4}
TOKEN_DAYS_DEFAULT = 90
SESSION_COOKIE = "sf_session"


class ModeError(RuntimeError):
    pass


class AuthError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    email: str
    role: str
    via: Literal["local", "iap", "token", "cookie"]


# ---- mode safety ----


def gce_metadata_reachable(timeout_s: float = 0.5) -> bool:
    try:
        r = httpx.get(GCE_METADATA_URL, headers={"Metadata-Flavor": "Google"}, timeout=timeout_s)
    except httpx.HTTPError:
        return False
    return r.headers.get("Metadata-Flavor") == "Google" or r.is_success


def resolve_mode(settings: Settings, *, on_gce: Callable[[], bool]) -> FactoryMode:
    if "mode" not in settings.model_fields_set:
        raise ModeError("FACTORY_MODE must be set explicitly to 'personal' or 'server'")
    if settings.mode is FactoryMode.PERSONAL:
        if settings.iap_audience:
            raise ModeError("personal mode refused: FACTORY_IAP_AUDIENCE is set")
        if on_gce():
            raise ModeError("personal mode refused: running on Google Compute Engine")
    return settings.mode


_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def check_bind_host(settings: Settings, host: str) -> None:
    if settings.mode is not FactoryMode.PERSONAL or settings.in_container:
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() in _LOOPBACK
    if not loopback:
        raise ValueError(f"personal mode refuses to bind {host!r}; use 127.0.0.1")


# ---- tokens ----


def new_token() -> str:
    return "sfp_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_usable(
    *, revoked_at: datetime | None, expires_at: datetime | None, now: datetime
) -> bool:
    return revoked_at is None and (expires_at is None or expires_at > now)


def load_personal_token(settings: Settings) -> str:
    if settings.api_token is not None:
        value = settings.api_token.get_secret_value().strip()
        if value:
            return value
    path = settings.api_token_file
    if path.is_file():
        value = path.read_text().strip()
        if value:
            return value
    raise ModeError(
        "personal mode needs FACTORY_API_TOKEN or a token file "
        f"({settings.api_token_file}); create one with: factory token init"
    )


def session_value(token: str) -> str:
    return hmac.new(token.encode(), b"session", hashlib.sha256).hexdigest()


@dataclass
class LoginCodes:
    clock: Callable[[], float] = time.monotonic
    _codes: dict[str, float] = field(default_factory=dict)

    def issue(self, *, ttl_s: int = 600) -> str:
        now = self.clock()
        self._codes = {c: t for c, t in self._codes.items() if t > now}
        code = secrets.token_urlsafe(24)
        self._codes[code] = now + ttl_s
        return code

    def redeem(self, code: str) -> bool:
        expires = self._codes.pop(code, None)
        return expires is not None and expires > self.clock()


# ---- origin ----


def _origin_key(value: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(value.strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if scheme not in ("http", "https") or not host:
            return None
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, host, port


def origin_allowed(origin: str | None, settings: Settings) -> bool:
    if origin is None:
        return True
    key = _origin_key(origin)
    if key is None:
        return False
    allowed = {_origin_key(settings.api_url)}
    if settings.public_url:
        allowed.add(_origin_key(settings.public_url))
    return key in allowed


# ---- IAP ----


def verify_iap_assertion(
    assertion: str, *, audience: str, certs: Mapping[str, str]
) -> Mapping[str, Any]:
    try:
        header = google_jwt.decode_header(assertion)
        if header.get("alg") != "ES256":
            raise AuthError("unexpected signing algorithm")
        claims = google_jwt.decode(assertion, certs=dict(certs), audience=audience)
    except AuthError:
        raise
    except Exception as e:
        raise AuthError(f"invalid IAP assertion: {e}") from e
    if claims.get("iss") != IAP_ISSUER:
        raise AuthError("unexpected issuer")
    return claims


class IapKeys:
    def __init__(self, url: str = IAP_CERTS_URL, ttl_s: float = 3600) -> None:
        self._url = url
        self._ttl = ttl_s
        self._certs: Mapping[str, str] | None = None
        self._at = 0.0

    def _fetch(self) -> Mapping[str, str]:
        r = httpx.get(self._url, timeout=5)
        r.raise_for_status()
        return r.json()

    async def certs(self) -> Mapping[str, str]:
        if self._certs is None or time.monotonic() - self._at > self._ttl:
            self._certs = await asyncio.to_thread(self._fetch)
            self._at = time.monotonic()
        return self._certs


# ---- directory ----


class UserDirectory:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def role_of(self, email: str) -> str | None:
        async with self.sessions() as s:
            return await s.scalar(select(User.role).where(User.email == email))

    async def token_email(self, token_hash: str) -> str | None:
        now = datetime.now(UTC)
        async with self.sessions() as s, s.begin():
            row = (
                await s.execute(
                    select(
                        ApiToken.id, ApiToken.email, ApiToken.revoked_at, ApiToken.expires_at
                    ).where(ApiToken.token_hash == token_hash)
                )
            ).first()
            if row is None or not token_usable(
                revoked_at=row.revoked_at, expires_at=row.expires_at, now=now
            ):
                return None
            await s.execute(update(ApiToken).where(ApiToken.id == row.id).values(last_used_at=now))
            return row.email


# ---- request principal ----


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="unauthorized")


async def current_principal(request: Request) -> Principal:
    state = request.app.state
    settings: Settings = state.settings
    bearer = _bearer(request)
    if state.mode is FactoryMode.PERSONAL:
        token: str = state.personal_token
        if bearer is not None and hmac.compare_digest(bearer.encode(), token.encode()):
            return Principal(settings.local_user, "admin", "token")
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie and hmac.compare_digest(cookie.encode(), session_value(token).encode()):
            return Principal(settings.local_user, "admin", "cookie")
        raise _unauthorized()
    users: UserDirectory = state.deps.users
    if bearer is not None:
        email = await users.token_email(hash_token(bearer))
        if email is None:
            raise _unauthorized()
        via: Literal["iap", "token"] = "token"
    else:
        assertion = request.headers.get("x-goog-iap-jwt-assertion")
        if not assertion:
            raise _unauthorized()
        assert settings.iap_audience
        try:
            claims = verify_iap_assertion(
                assertion, audience=settings.iap_audience, certs=await state.iap_keys.certs()
            )
        except (AuthError, OSError, httpx.HTTPError) as e:
            log.info("auth.iap_refused", error=str(e))
            raise _unauthorized() from e
        email = str(claims.get("email") or "")
        if not email:
            raise _unauthorized()
        via = "iap"
    role = await users.role_of(email)
    if role is None:
        raise HTTPException(status_code=403, detail="user not registered")
    return Principal(email, role, via)


def require(role: str) -> Callable[..., Awaitable[Principal]]:
    need = ROLE_RANK[role]

    async def dependency(request: Request) -> Principal:
        principal = await current_principal(request)
        if ROLE_RANK.get(principal.role, 0) < need:
            raise HTTPException(status_code=403, detail=f"requires role {role}")
        return principal

    return dependency
