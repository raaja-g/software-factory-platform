"""Async GitHub REST/GraphQL client with a serial, rate-limit aware mutation queue.

Uses httpx directly (githubkit 0.17 ships its own HTTP stack, httpx2, which the
respx mocks cannot intercept); the queue alone owns retries and ETags are sent by hand.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeVar
from urllib.parse import quote

import httpx

from swfactory.github.auth import PERMS_DELIVER, PERMS_FETCH, GitHubAuth

T = TypeVar("T")

API = "https://api.github.com"
BACKOFF_S = 60.0
TRIES = 3


class RateLimited(Exception):
    def __init__(self, status: int, headers: Mapping[str, str]) -> None:
        super().__init__(f"rate limited ({status})")
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.message = message


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class MutationQueue:
    """One lock per key (the installation); at least ``min_interval_s`` between mutations."""

    def __init__(self, min_interval_s: float = 1.0, clock: Clock | None = None) -> None:
        self._min = min_interval_s
        self._clock: Clock = clock or RealClock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}

    async def _wait_blocked(self, key: str) -> None:
        wait = self._blocked_until.get(key, 0.0) - self._clock.now()
        if wait > 0:
            await self._clock.sleep(wait)

    def _wait_for(self, exc: RateLimited, attempt: int) -> float:
        h = exc.headers
        if "retry-after" in h:
            try:
                return max(0.0, float(h["retry-after"]))
            except ValueError:
                pass
        if h.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in h:
            try:
                return max(0.0, float(h["x-ratelimit-reset"]) - self._clock.now())
            except ValueError:
                pass
        return BACKOFF_S * (2**attempt)

    async def _attempts(self, key: str, call: Callable[[], Awaitable[T]], *, spaced: bool) -> T:
        for attempt in range(TRIES):
            await self._wait_blocked(key)
            if spaced:
                gap = self._last.get(key, float("-inf")) + self._min - self._clock.now()
                if gap > 0:
                    await self._clock.sleep(gap)
            try:
                result = await call()
            except RateLimited as exc:
                if spaced:
                    self._last[key] = self._clock.now()
                if attempt == TRIES - 1:
                    raise
                self._blocked_until[key] = self._clock.now() + self._wait_for(exc, attempt)
                continue
            if spaced:
                self._last[key] = self._clock.now()
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    async def run(self, key: str, call: Callable[[], Awaitable[T]]) -> T:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._attempts(key, call, spaced=True)

    async def read(self, key: str, call: Callable[[], Awaitable[T]]) -> T:
        """Reads skip the lock and spacing but share the retry rules and the rate-limit wait."""
        return await self._attempts(key, call, spaced=False)


@dataclass(frozen=True, slots=True)
class PullRef:
    number: int
    node_id: str
    url: str
    head_sha: str
    state: Literal["open", "closed"]
    merged: bool
    draft: bool
    merge_sha: str | None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IssueComment:
    id: int
    login: str
    user_type: str
    body: str
    created_at: datetime
    updated_at: datetime | None = None
    issue_number: int | None = None


@dataclass(frozen=True, slots=True)
class LabelEvent:
    login: str
    user_type: str
    at: datetime


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _pull(d: Mapping[str, Any]) -> PullRef:
    merged = bool(d.get("merged") or d.get("merged_at"))
    return PullRef(
        number=int(d["number"]),
        node_id=str(d.get("node_id", "")),
        url=str(d.get("html_url", "")),
        head_sha=str(d["head"]["sha"]),
        state="closed" if d.get("state") == "closed" else "open",
        merged=merged,
        draft=bool(d.get("draft", False)),
        merge_sha=(d.get("merge_commit_sha") or None) if merged else None,
        updated_at=_dt(d.get("updated_at")),
    )


def _comment(d: Mapping[str, Any]) -> IssueComment:
    user = d.get("user") or {}
    issue_url = str(d.get("issue_url") or "")
    tail = issue_url.rsplit("/", 1)[-1]
    created = _dt(d.get("created_at")) or datetime.fromtimestamp(0, UTC)
    return IssueComment(
        id=int(d["id"]),
        login=str(user.get("login", "")),
        user_type=str(user.get("type", "")),
        body=str(d.get("body") or ""),
        created_at=created,
        updated_at=_dt(d.get("updated_at")),
        issue_number=int(tail) if tail.isdigit() else None,
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_MARK_READY = (
    "mutation($pullRequestId: ID!) { markPullRequestReadyForReview(input: "
    "{pullRequestId: $pullRequestId}) { pullRequest { id } } }"
)
_AUTO_MERGE = (
    "mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!, "
    "$expectedHeadOid: GitObjectID!) { enablePullRequestAutoMerge(input: "
    "{pullRequestId: $pullRequestId, mergeMethod: $mergeMethod, "
    "expectedHeadOid: $expectedHeadOid}) { pullRequest { id } } }"
)


class GitHubClient:
    def __init__(
        self,
        auth: GitHubAuth,
        repo: str,
        queue: MutationQueue,
        *,
        http: httpx.AsyncClient | None = None,
        api_url: str = API,
    ) -> None:
        owner, _, name = repo.partition("/")
        if not owner or not name:
            raise ValueError(f"repo must be owner/name, got {repo!r}")
        self._auth = auth
        self._repo = repo
        self._owner = owner
        self._queue = queue
        self._http = http or httpx.AsyncClient(timeout=30)
        self._api = api_url.rstrip("/")

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- transport -----------------------------------------------------------------

    @property
    def _p(self) -> str:
        return f"/repos/{self._repo}"

    async def _send(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        etag: str | None = None,
        headers: Mapping[str, str] | None = None,
        ok: tuple[int, ...] = (),
    ) -> httpx.Response:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if headers is None:
            token = await self._auth.token_for(self._repo, PERMS_DELIVER)
            h["Authorization"] = f"Bearer {token}"
        else:
            h.update(headers)
        if etag:
            h["If-None-Match"] = etag
        url = path_or_url if path_or_url.startswith("http") else f"{self._api}{path_or_url}"
        r = await self._http.request(method, url, params=params, json=json, headers=h)
        if r.status_code in ok or r.status_code == 304 and etag:
            return r
        if r.is_success:
            return r
        text = r.text[:500]
        if r.status_code == 429 or (
            r.status_code == 403
            and (
                "retry-after" in r.headers
                or r.headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in text.lower()
            )
        ):
            raise RateLimited(r.status_code, dict(r.headers))
        raise GitHubError(r.status_code, text)

    async def _read(self, method: str, path: str, **kw: Any) -> httpx.Response:
        return await self._queue.read(self._owner, lambda: self._send(method, path, **kw))

    async def _mutate(self, method: str, path: str, **kw: Any) -> httpx.Response:
        return await self._queue.run(self._owner, lambda: self._send(method, path, **kw))

    async def _pages(self, path: str, params: Mapping[str, Any]) -> list[Any]:
        items: list[Any] = []
        target: str | None = path
        first = True
        while target:
            r = await self._read("GET", target, params=params if first else None)
            first = False
            items.extend(r.json())
            target = r.links.get("next", {}).get("url")
        return items

    async def _graphql(self, query: str, variables: Mapping[str, Any]) -> None:
        r = await self._mutate("POST", "/graphql", json={"query": query, "variables": variables})
        errors = r.json().get("errors")
        if errors:
            raise GitHubError(200, str(errors)[:500])

    # -- pull requests -------------------------------------------------------------

    async def find_pr(self, branch: str) -> PullRef | None:
        r = await self._read(
            "GET",
            f"{self._p}/pulls",
            params={"head": f"{self._owner}:{branch}", "state": "all", "per_page": 100},
        )
        prs = [_pull(d) for d in r.json()]
        for pr in prs:
            if pr.state == "open":
                return pr
        return prs[0] if prs else None

    async def get_pr(self, number: int) -> PullRef:
        return _pull((await self._read("GET", f"{self._p}/pulls/{number}")).json())

    async def create_pr(
        self, *, head: str, base: str, title: str, body: str, draft: bool = True
    ) -> PullRef:
        r = await self._mutate(
            "POST",
            f"{self._p}/pulls",
            json={"head": head, "base": base, "title": title, "body": body, "draft": draft},
        )
        return _pull(r.json())

    async def update_pr_body(self, number: int, body: str) -> None:
        await self._mutate("PATCH", f"{self._p}/pulls/{number}", json={"body": body})

    async def close_pr(self, number: int) -> None:
        await self._mutate("PATCH", f"{self._p}/pulls/{number}", json={"state": "closed"})

    async def mark_ready(self, pr: PullRef) -> None:
        await self._graphql(_MARK_READY, {"pullRequestId": pr.node_id})

    async def enable_auto_merge(self, pr: PullRef, *, expected_head_oid: str, method: str) -> None:
        await self._graphql(
            _AUTO_MERGE,
            {
                "pullRequestId": pr.node_id,
                "mergeMethod": method.upper(),
                "expectedHeadOid": expected_head_oid,
            },
        )

    async def merge_async(self, number: int, *, sha: str, method: str) -> str:
        r = await self._mutate(
            "PUT",
            f"{self._p}/pulls/{number}/merge-async",
            json={"sha": sha, "merge_method": method, "merge_action": "default"},
        )
        data = r.json()
        return str(data.get("uuid") or data.get("id") or "")

    async def merge_async_status(self, number: int, uuid: str) -> str:
        r = await self._read("GET", f"{self._p}/pulls/{number}/merge-async/{uuid}")
        return str(r.json().get("status", ""))

    async def list_prs(
        self, *, etag: str | None, page: int = 1
    ) -> tuple[list[PullRef], str | None, bool]:
        r = await self._read(
            "GET",
            f"{self._p}/pulls",
            params={
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            },
            etag=etag,
        )
        if r.status_code == 304:
            return [], r.headers.get("etag", etag), True
        return [_pull(d) for d in r.json()], r.headers.get("etag"), False

    # -- issues, labels, comments --------------------------------------------------

    async def add_labels(self, number: int, labels: list[str]) -> None:
        path = f"{self._p}/issues/{number}/labels"
        try:
            await self._mutate("POST", path, json={"labels": labels})
            return
        except GitHubError as exc:
            if exc.status != 422:
                raise
        for name in labels:
            try:
                await self._mutate("POST", f"{self._p}/labels", json={"name": name})
            except GitHubError as exc:
                if exc.status != 422:  # 422: already exists
                    raise
        await self._mutate("POST", path, json={"labels": labels})

    async def remove_label(self, number: int, label: str) -> None:
        try:
            await self._mutate(
                "DELETE", f"{self._p}/issues/{number}/labels/{quote(label, safe='')}"
            )
        except GitHubError as exc:
            if exc.status != 404:
                raise

    async def comment_once(self, number: int, body: str, *, marker: str) -> bool:
        existing = await self._pages(f"{self._p}/issues/{number}/comments", {"per_page": 100})
        if any(marker in str(c.get("body") or "") for c in existing):
            return False
        await self._mutate("POST", f"{self._p}/issues/{number}/comments", json={"body": body})
        return True

    async def get_comment(self, comment_id: int) -> IssueComment:
        r = await self._read("GET", f"{self._p}/issues/comments/{comment_id}")
        return _comment(r.json())

    async def list_issues(
        self, *, label: str, etag: str | None
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        r = await self._read(
            "GET",
            f"{self._p}/issues",
            params={"labels": label, "state": "all", "per_page": 100},
            etag=etag,
        )
        if r.status_code == 304:
            return [], r.headers.get("etag", etag), True
        items = list(r.json())
        nxt = r.links.get("next", {}).get("url")
        while nxt:
            r2 = await self._read("GET", nxt)
            items.extend(r2.json())
            nxt = r2.links.get("next", {}).get("url")
        return [i for i in items if "pull_request" not in i], r.headers.get("etag"), False

    async def list_issue_comments(
        self, *, since: datetime, etag: str | None
    ) -> tuple[list[IssueComment], str | None, bool]:
        params = {"since": _iso(since), "sort": "updated", "per_page": 100}
        r = await self._read("GET", f"{self._p}/issues/comments", params=params, etag=etag)
        if r.status_code == 304:
            return [], r.headers.get("etag", etag), True
        items = list(r.json())
        nxt = r.links.get("next", {}).get("url")
        while nxt:
            r2 = await self._read("GET", nxt)
            items.extend(r2.json())
            nxt = r2.links.get("next", {}).get("url")
        return [_comment(c) for c in items], r.headers.get("etag"), False

    async def issue_label_event(self, number: int, label: str) -> LabelEvent | None:
        events = await self._pages(f"{self._p}/issues/{number}/events", {"per_page": 100})
        found: LabelEvent | None = None
        for e in events:
            if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label:
                actor = e.get("actor") or {}
                at = _dt(e.get("created_at"))
                if at is not None:
                    found = LabelEvent(str(actor.get("login", "")), str(actor.get("type", "")), at)
        return found

    # -- checks, repo ----------------------------------------------------------------

    async def _find_check(self, head_sha: str, name: str) -> dict[str, Any] | None:
        r = await self._read(
            "GET",
            f"{self._p}/commits/{head_sha}/check-runs",
            params={"check_name": name, "per_page": 100},
        )
        for run in r.json().get("check_runs", []):
            if run.get("name") == name:
                return dict(run)
        return None

    async def has_check_run(self, head_sha: str, name: str) -> bool:
        return await self._find_check(head_sha, name) is not None

    async def upsert_check_run(
        self,
        *,
        head_sha: str,
        name: str,
        conclusion: str,
        title: str,
        summary: str,
        details_url: str,
    ) -> int:
        payload: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary},
            "details_url": details_url,
        }
        existing = await self._find_check(head_sha, name)
        if existing:
            r = await self._mutate("PATCH", f"{self._p}/check-runs/{existing['id']}", json=payload)
        else:
            r = await self._mutate("POST", f"{self._p}/check-runs", json=payload)
        return int(r.json()["id"])

    async def collaborator_permission(self, login: str) -> str:
        try:
            r = await self._read(
                "GET", f"{self._p}/collaborators/{quote(login, safe='')}/permission"
            )
        except GitHubError as exc:
            if exc.status == 404:
                return "none"
            raise
        return str(r.json().get("permission", "none"))

    async def branch_sha(self, branch: str) -> str | None:
        try:
            r = await self._read("GET", f"{self._p}/git/ref/heads/{quote(branch, safe='/')}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return str(r.json()["object"]["sha"])

    async def branch_rules(self, branch: str) -> list[dict[str, Any]]:
        r = await self._read("GET", f"{self._p}/rules/branches/{quote(branch, safe='')}")
        return list(r.json())

    async def bot_identity(self) -> tuple[str, str]:
        headers_fn = getattr(self._auth, "app_headers", None)
        if headers_fn is None:
            raise GitHubError(0, "bot identity needs a GitHub App")
        app = (await self._read("GET", "/app", headers=headers_fn())).json()
        slug = str(app["slug"])
        user = (await self._read("GET", f"/users/{slug}[bot]")).json()
        return f"{slug}[bot]", f"{user['id']}+{slug}[bot]@users.noreply.github.com"

    # -- provisioning (new repos; uses the org token, since the repo may not exist yet) ---

    async def _org_headers(self, permissions: Mapping[str, str]) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._auth.org_token(self._owner, permissions)}"}

    async def repo_info(self, permissions: Mapping[str, str]) -> dict[str, Any] | None:
        """``GET /repos/{repo}``: the repo, or None when it does not exist."""
        try:
            r = await self._read("GET", self._p, headers=await self._org_headers(permissions))
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return dict(r.json())

    async def owner_is_user(self) -> bool:
        r = await self._read("GET", f"/users/{quote(self._owner, safe='')}")
        return str(r.json().get("type", "")) == "User"

    async def create_org_repo(
        self, *, private: bool, permissions: Mapping[str, str], user_owned: bool = False
    ) -> dict[str, Any]:
        """``POST /orgs/{org}/repos`` (``POST /user/repos`` for a user-owned repo, PAT only)."""
        name = self._repo.partition("/")[2]
        body = {
            "name": name,
            "private": private,
            "auto_init": True,
            "allow_auto_merge": True,
            "delete_branch_on_merge": True,
        }
        path = "/user/repos" if user_owned else f"/orgs/{self._owner}/repos"
        r = await self._mutate(
            "POST", path, json=body, headers=await self._org_headers(permissions)
        )
        return dict(r.json())

    async def generate_from_template(
        self, template: str, *, private: bool, permissions: Mapping[str, str]
    ) -> dict[str, Any]:
        """Generate the repo from a template repository, then turn on auto-merge for it."""
        name = self._repo.partition("/")[2]
        headers = await self._org_headers(permissions)
        r = await self._mutate(
            "POST",
            f"/repos/{template}/generate",
            json={
                "owner": self._owner,
                "name": name,
                "private": private,
                "include_all_branches": False,
            },
            headers=headers,
        )
        data = dict(r.json())
        patch = {"allow_auto_merge": True, "delete_branch_on_merge": True}
        for attempt in range(4):  # generation is asynchronous; the repo may not be there yet
            try:
                await self._mutate("PATCH", self._p, json=patch, headers=headers)
                break
            except GitHubError as exc:
                if exc.status != 404 or attempt == 3:
                    raise
                await asyncio.sleep(1 + attempt)
        return data

    async def get_repo(self) -> dict[str, Any] | None:
        """``GET /repos/{repo}`` with an installation token (read-only); None on 404."""
        try:
            token = await self._auth.token_for(self._repo, PERMS_FETCH)
            r = await self._read("GET", self._p, headers={"Authorization": f"Bearer {token}"})
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return dict(r.json())

    async def installation_reachable(self) -> bool:
        """Is the repo in the App's installation? ``GET /repos/{r}/installation`` with the JWT.

        Without an App (a personal-mode PAT) every repo the token can see is reachable.
        """
        headers_fn = getattr(self._auth, "app_headers", None)
        if headers_fn is None:
            return True
        try:
            await self._read("GET", f"{self._p}/installation", headers=headers_fn())
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise
        return True
