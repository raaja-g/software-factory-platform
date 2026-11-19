"""A respx-backed GitHub REST stand-in for one repo: reachability, default branch, emptiness."""

from __future__ import annotations

from typing import Any

import httpx
import respx

API = "https://api.github.com"


class FakeGhApi:
    def __init__(self, repo: str = "acme/web") -> None:
        self.repo = repo
        self.installed = True
        self.empty = False
        self.default_branch = "trunk"
        self.calls: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        base = f"/repos/{self.repo}"
        if request.method != "GET":
            return httpx.Response(405, json={})
        if path == base + "/installation":
            return httpx.Response(200 if self.installed else 404, json={"id": 1})
        if path == base:
            return httpx.Response(200, json={"default_branch": self.default_branch})
        if path == f"{base}/git/ref/heads/{self.default_branch}":
            if self.empty:
                return httpx.Response(409, json={"message": "Git Repository is empty."})
            return httpx.Response(200, json={"object": {"sha": "a" * 40}})
        return httpx.Response(404, json={})


class StubAuth:
    async def token_for(self, repo: str, permissions: Any) -> str:
        return "tok"

    async def org_token(self, org: str, permissions: Any) -> str:
        return "tok"

    def app_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer jwt"}


def mock_github(api: FakeGhApi) -> respx.MockRouter:
    router = respx.mock(base_url=API, assert_all_called=False)
    router.route().mock(side_effect=api.handle)
    return router
