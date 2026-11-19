"""Token broker (orchestrator side) and BrokerAuth (api side).

Only the orchestrator holds the GitHub App private key. The internet-facing api asks the broker
for narrow, single-repo tokens: PRs on the definition repo, comments on app repos. Nothing else.
"""

import hmac
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from swfactory.definition.writer import GitHubDefinitionWriter
from swfactory.github.auth import (
    PERMS_COMMENT,
    PERMS_DEFINITION_PR,
    PERMS_PACKAGES_READ,
    AppAuth,
    GitHubAuth,
)
from swfactory.logging import register_secret

log = structlog.get_logger(__name__)

BROKER_CACHE_S = 50 * 60


class TokenBroker:
    def __init__(
        self,
        auth: AppAuth,
        *,
        definition_repo: str | None,
        app_repos: Callable[[], Awaitable[set[str]]],
    ) -> None:
        self._auth = auth
        self._definition_repo = definition_repo
        self._app_repos = app_repos

    async def _allowed(self, repo: str, permissions: Mapping[str, str]) -> bool:
        perms = dict(permissions)
        if self._definition_repo is not None and repo == self._definition_repo:
            if perms == PERMS_DEFINITION_PR:
                return True
        if perms == PERMS_COMMENT and repo in await self._app_repos():
            return True
        return False

    async def issue(self, repo: str, permissions: Mapping[str, str]) -> str:
        if not await self._allowed(repo, permissions):
            log.warning("broker.refused", repo=repo, permissions=sorted(permissions))
            raise PermissionError(f"the broker does not issue that token for {repo}")
        token = await self._auth.token_for(repo, permissions)
        log.info("broker.issued", repo=repo, permissions=sorted(permissions.items()))
        return token

    async def issue_packages_read(self, org: str) -> str:
        owners: set[str] = {r.partition("/")[0] for r in await self._app_repos()}
        if self._definition_repo is not None:
            owners.add(self._definition_repo.partition("/")[0])
        if not org or org not in owners:
            log.warning("broker.refused", org=org, permissions=sorted(PERMS_PACKAGES_READ))
            raise PermissionError(f"the broker does not issue package tokens for {org}")
        token = await self._auth.org_token(org, PERMS_PACKAGES_READ)
        log.info("broker.issued", org=org, permissions=sorted(PERMS_PACKAGES_READ.items()))
        return token


def broker_app(broker: TokenBroker, *, shared_token: str) -> Starlette:
    expected = f"Bearer {shared_token}".encode()

    async def issue(request: Request) -> JSONResponse:
        given = request.headers.get("authorization", "").encode()
        if not shared_token or not hmac.compare_digest(given, expected):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            repo = body["repo"]
            perms = body["permissions"]
            if not isinstance(repo, str) or not isinstance(perms, dict):
                raise TypeError
        except Exception:
            return JSONResponse({"detail": "bad request"}, status_code=400)
        try:
            token = await broker.issue(repo, perms)
        except PermissionError:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        except Exception as exc:
            log.warning("broker.mint_failed", repo=repo, error=type(exc).__name__)
            return JSONResponse({"detail": "mint failed"}, status_code=502)
        return JSONResponse({"token": token})

    async def packages(request: Request) -> JSONResponse:
        given = request.headers.get("authorization", "").encode()
        if not shared_token or not hmac.compare_digest(given, expected):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            org = (await request.json())["org"]
            if not isinstance(org, str):
                raise TypeError
        except Exception:
            return JSONResponse({"detail": "bad request"}, status_code=400)
        try:
            token = await broker.issue_packages_read(org)
        except PermissionError:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        except Exception as exc:
            log.warning("broker.mint_failed", org=org, error=type(exc).__name__)
            return JSONResponse({"detail": "mint failed"}, status_code=502)
        return JSONResponse({"token": token})

    return Starlette(
        routes=[
            Route("/internal/github-token", issue, methods=["POST"]),
            Route("/internal/github-packages-token", packages, methods=["POST"]),
        ]
    )


class BrokerAuth:
    """GitHubAuth for the api: asks the broker; never holds the App key."""

    def __init__(
        self,
        url: str,
        token_file: str | Path,
        http: httpx.AsyncClient | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url.rstrip("/")
        self._token_file = Path(token_file)
        self._http = http or httpx.AsyncClient(timeout=30)
        self._clock = clock
        self._cache: dict[tuple[str, frozenset[tuple[str, str]]], tuple[str, float]] = {}

    async def token_for(self, repo: str, permissions: Mapping[str, str]) -> str:
        key = (repo, frozenset(permissions.items()))
        hit = self._cache.get(key)
        if hit and hit[1] > self._clock():
            return hit[0]
        shared = self._token_file.read_text().strip()
        r = await self._http.post(
            f"{self._url}/internal/github-token",
            json={"repo": repo, "permissions": dict(permissions)},
            headers={"authorization": f"Bearer {shared}"},
        )
        r.raise_for_status()
        token = str(r.json()["token"])
        register_secret(token)
        self._cache[key] = (token, self._clock() + BROKER_CACHE_S)
        return token

    async def org_token(self, org: str, permissions: Mapping[str, str]) -> str:
        raise PermissionError("the api cannot mint organisation tokens")


class _RestWriterClient:
    """The slice of the GitHub REST API that GitHubDefinitionWriter uses."""

    def __init__(self, token: str, http: httpx.AsyncClient, api_url: str) -> None:
        self._h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._http = http
        self._api = api_url.rstrip("/")

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._http.request(method, f"{self._api}{path}", headers=self._h, **kw)
        r.raise_for_status()
        return r

    async def default_branch(self, repo: str) -> str:
        return str((await self._req("GET", f"/repos/{repo}")).json()["default_branch"])

    async def get_branch_sha(self, repo: str, branch: str) -> str:
        r = await self._req("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        return str(r.json()["object"]["sha"])

    async def get_file_sha(self, repo: str, path: str, ref: str) -> str | None:
        try:
            r = await self._req("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return str(r.json()["sha"])

    async def create_ref(self, repo: str, ref: str, sha: str) -> Any:
        return (
            await self._req("POST", f"/repos/{repo}/git/refs", json={"ref": ref, "sha": sha})
        ).json()

    async def create_or_update_file_contents(
        self,
        repo: str,
        path: str,
        *,
        message: str,
        content: str,
        branch: str,
        sha: str | None,
        author: str,
    ) -> Any:
        body: dict[str, Any] = {"message": message, "content": content, "branch": branch}
        if sha:
            body["sha"] = sha
        return (await self._req("PUT", f"/repos/{repo}/contents/{path}", json=body)).json()

    async def create_pull(self, repo: str, *, title: str, head: str, base: str, body: str) -> str:
        r = await self._req(
            "POST",
            f"/repos/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        return str(r.json()["html_url"])


async def github_for(
    auth: GitHubAuth,
    repo: str,
    permissions: Mapping[str, str],
    *,
    http: httpx.AsyncClient | None = None,
    api_url: str = "https://api.github.com",
) -> _RestWriterClient:
    token = await auth.token_for(repo, permissions)
    return _RestWriterClient(token, http or httpx.AsyncClient(timeout=30), api_url)


class ServerDefinitionWriter(GitHubDefinitionWriter):
    """GitHubDefinitionWriter opening PRs on the default branch (looked up lazily)."""

    def __init__(self, auth: GitHubAuth, repo: str, **kw: Any) -> None:
        self._auth = auth
        super().__init__(self._client, repo, "main", **kw)

    async def _client(self) -> Any:
        gh = await github_for(self._auth, self._repo, PERMS_DEFINITION_PR)
        self._base = await gh.default_branch(self._repo)
        return gh
