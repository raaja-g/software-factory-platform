"""GitHub auth: App JWT, installation tokens narrowed to one repo, PAT for personal mode."""

import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx
import jwt

from swfactory.domain.types import FactoryMode
from swfactory.logging import register_secret
from swfactory.settings import Settings

PERMS_DELIVER = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "write",
    "metadata": "read",
}
PERMS_PUSH = {"contents": "write", "metadata": "read"}
PERMS_FETCH = {"contents": "read", "metadata": "read"}
PERMS_COMMENT = {
    "issues": "write",
    "pull_requests": "read",
    "checks": "read",
    "metadata": "read",
}
PERMS_DEFINITION_PR = {"contents": "write", "pull_requests": "write", "metadata": "read"}
PERMS_PACKAGES_READ = {"packages": "read", "metadata": "read"}

API = "https://api.github.com"
CACHE_S = 55 * 60
EXPIRY_MARGIN_S = 5 * 60


def app_jwt(client_id: str, private_key_pem: str, *, now: int) -> str:
    claims = {"iat": now - 60, "exp": now + 540, "iss": client_id}
    return jwt.encode(claims, private_key_pem, algorithm="RS256")


class GitHubAuth(Protocol):
    async def token_for(self, repo: str, permissions: Mapping[str, str]) -> str: ...

    async def org_token(self, org: str, permissions: Mapping[str, str]) -> str: ...


class AppAuth:
    instances = 0  # constructions in this process; tests assert the api never builds one

    def __init__(
        self,
        client_id: str,
        private_key_pem: str,
        http: httpx.AsyncClient | None = None,
        *,
        clock: Callable[[], float] = time.time,
        api_url: str = API,
    ) -> None:
        AppAuth.instances += 1
        self._client_id = client_id
        self._key = private_key_pem
        self._http = http or httpx.AsyncClient(timeout=30)
        self._clock = clock
        self._api = api_url.rstrip("/")
        self._cache: dict[tuple[str, frozenset[tuple[str, str]]], tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        token = app_jwt(self._client_id, self._key, now=int(self._clock()))
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def app_headers(self) -> dict[str, str]:
        """Headers carrying the App JWT, for the few endpoints that need it (``GET /app``)."""
        return self._headers()

    async def _mint(self, installation_path: str, body: dict[str, object]) -> tuple[str, float]:
        r = await self._http.get(f"{self._api}{installation_path}", headers=self._headers())
        r.raise_for_status()
        inst = r.json()["id"]
        r = await self._http.post(
            f"{self._api}/app/installations/{inst}/access_tokens",
            headers=self._headers(),
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        token = str(data["token"])
        register_secret(token)
        now = self._clock()
        expires = now + CACHE_S
        if data.get("expires_at"):
            exp = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00"))
            expires = min(expires, exp.timestamp() - EXPIRY_MARGIN_S)
        return token, expires

    async def _cached(
        self,
        key: tuple[str, frozenset[tuple[str, str]]],
        path: str,
        body: dict[str, object],
    ) -> str:
        async with self._lock:
            hit = self._cache.get(key)
            if hit and hit[1] > self._clock():
                return hit[0]
            token, expires = await self._mint(path, body)
            self._cache[key] = (token, expires)
            return token

    async def token_for(self, repo: str, permissions: Mapping[str, str]) -> str:
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name:
            raise ValueError(f"repo must be owner/name, got {repo!r}")
        return await self._cached(
            (repo, frozenset(permissions.items())),
            f"/repos/{owner}/{name}/installation",
            {"repositories": [name], "permissions": dict(permissions)},
        )

    async def org_token(self, org: str, permissions: Mapping[str, str]) -> str:
        return await self._cached(
            (f"org:{org}", frozenset(permissions.items())),
            f"/orgs/{org}/installation",
            {"permissions": dict(permissions)},
        )


class PatAuth:
    """A personal access token (personal mode only). Permissions are ignored."""

    def __init__(self, token: str) -> None:
        self._token = token
        register_secret(token)

    async def token_for(self, repo: str, permissions: Mapping[str, str]) -> str:
        return self._token

    async def org_token(self, org: str, permissions: Mapping[str, str]) -> str:
        return self._token


def make_auth(settings: Settings, http: httpx.AsyncClient | None = None) -> GitHubAuth:
    if settings.github_app_client_id:
        key_file = settings.github_app_private_key_file
        if key_file is None:
            raise ValueError("FACTORY_GITHUB_APP_PRIVATE_KEY_FILE is required with a GitHub App")
        return AppAuth(settings.github_app_client_id, Path(key_file).read_text(), http)
    if settings.mode == FactoryMode.PERSONAL and settings.github_token:
        return PatAuth(settings.github_token.get_secret_value())
    if settings.github_token:
        raise ValueError(
            "a GitHub PAT is only allowed with FACTORY_MODE=personal; use a GitHub App"
        )
    raise ValueError(
        "no GitHub credentials: set FACTORY_GITHUB_APP_CLIENT_ID "
        "or, in personal mode, FACTORY_GITHUB_TOKEN"
    )
