"""Definition sync (orchestrator side): push, poll and CLI trigger one `sync_now`; the PR check.

The api never writes `definitions` or `apps`: its `push` / `pull_request` webhook handlers and
`POST /api/definition/sync` only `pg_notify('definition_sync', ...)`, and `DefinitionSync.listen`
in the worker (which holds the App key) does the work."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from swfactory.definition.loader import (
    GIT_FETCH,
    DefinitionError,
    GitFetch,
    LoadResult,
    load_definition,
)
from swfactory.definition.store import NoActiveDefinition
from swfactory.domain.types import FactoryMode
from swfactory.github.auth import PERMS_FETCH, GitHubAuth, PatAuth

if TYPE_CHECKING:
    from swfactory.definition.store import DefinitionStore
    from swfactory.github.client import GitHubClient
    from swfactory.settings import Settings

log = logging.getLogger(__name__)

CHANNEL = "definition_sync"
DEFINITION_CHECK = "factory/definition"
UNPROTECTED_DEFINITION = (
    "the definition repo's default branch has no ruleset requiring a pull request with one approval"
)
PR_ACTIONS = frozenset({"opened", "synchronize", "reopened"})
_GH_SOURCE = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_SUMMARY_LIMIT = 60_000


def definition_repo_slug(source: str | None) -> str | None:
    """``owner/name`` for a GitHub URL, None for a local path or anything else."""
    if not source:
        return None
    m = _GH_SOURCE.match(source.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def definition_protected(rules: Sequence[Mapping[str, Any]]) -> bool:
    """True when a ``pull_request`` rule requires at least one approving review."""
    for rule in rules:
        if rule.get("type") != "pull_request":
            continue
        params = rule.get("parameters") or {}
        if int(params.get("required_approving_review_count") or 0) >= 1:
            return True
    return False


def definition_hook_payload(
    event: str, payload: Mapping[str, Any], *, definition_repo: str | None, default_branch: str
) -> dict[str, Any] | None:
    """What the api notifies for a GitHub webhook, or None when it is not about the definition.

    A ``push`` to the default branch asks for a sync; a PR opened, updated or reopened asks for
    the ``factory/definition`` check on its head commit."""
    repo = (payload.get("repository") or {}).get("full_name")
    if definition_repo is None or repo is None or repo.lower() != definition_repo.lower():
        return None
    if event == "push":
        if payload.get("ref") != f"refs/heads/{default_branch}":
            return None
        return {"reason": "push"}
    if event == "pull_request" and payload.get("action") in PR_ACTIONS:
        head = ((payload.get("pull_request") or {}).get("head") or {}).get("sha")
        if head:
            return {"reason": "pull_request", "head_sha": str(head)}
    return None


def definition_handler(event: str) -> Callable[[Any, Mapping[str, Any]], Any]:
    """A `GitHubEventHandlers` handler for ``push`` or ``pull_request``: it only
    `pg_notify('definition_sync')` (in the hook's transaction); the worker does the work."""
    from sqlalchemy import text

    async def handle(ctx: Any, payload: Mapping[str, Any]) -> None:
        note = definition_hook_payload(
            event,
            payload,
            definition_repo=definition_repo_slug(ctx.settings.definition_source),
            default_branch=ctx.settings.definition_ref,
        )
        if note is not None:
            await ctx.s.execute(
                text("select pg_notify(:ch, :p)"), {"ch": CHANNEL, "p": json.dumps(note)}
            )

    return handle


class DefinitionSync:
    def __init__(
        self,
        settings: Settings,
        store: DefinitionStore,
        *,
        auth: GitHubAuth | None = None,
        loader: Callable[..., LoadResult] = load_definition,
        client_factory: Callable[[str], GitHubClient] | None = None,
        dsn: str | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.auth = auth
        self._loader = loader
        self._client_factory = client_factory
        self._dsn = dsn
        self._lock = asyncio.Lock()
        self._seen: str | None = None  # last rejected sha, so a poll does not re-store it
        self._warned_checks = False

    # -- helpers -----------------------------------------------------------------

    @property
    def source(self) -> str | None:
        return self.settings.definition_source

    @property
    def repo(self) -> str | None:
        return definition_repo_slug(self.source)

    @property
    def branch(self) -> str:
        return self.settings.definition_ref

    def _client(self, repo: str) -> GitHubClient:
        if self._client_factory is not None:
            return self._client_factory(repo)
        from swfactory.github.client import GitHubClient, MutationQueue

        assert self.auth is not None
        return GitHubClient(self.auth, repo, MutationQueue())

    async def _fetch_options(self, repo: str) -> GitFetch | None:
        """Hardened git args and an env holding GIT_TOKEN (PERMS_FETCH); None without auth."""
        if self.auth is None:
            return None
        from swfactory.gitops.github import HARDENED_GIT_ENV, hardened_git_args

        token = await self.auth.token_for(repo, PERMS_FETCH)
        return GitFetch(tuple(hardened_git_args()), {**HARDENED_GIT_ENV, "GIT_TOKEN": token})

    async def _load(self, source: str, ref: str | None, mode: FactoryMode) -> LoadResult:
        repo = definition_repo_slug(source)
        fetch = await self._fetch_options(repo) if repo else None
        reset = GIT_FETCH.set(fetch)
        try:
            return await asyncio.to_thread(self._loader, source, ref=ref, mode=mode)
        finally:
            GIT_FETCH.reset(reset)

    async def _active_sha(self) -> str | None:
        try:
            sha, _ = await self.store.active()
        except NoActiveDefinition:
            return None
        return sha

    # -- sync --------------------------------------------------------------------

    async def sync_now(self, *, reason: str) -> str | None:
        """Load the source and activate it when it is new and valid. Returns the activated sha."""
        source = self.source
        if not source:
            log.info("definition sync (%s): FACTORY_DEFINITION is not set", reason)
            return None
        if self._lock.locked():  # a sync is in flight: push, poll and CLI calls make it one sync
            async with self._lock:
                return None
        async with self._lock:
            ref = None if self.branch == "main" else self.branch
            result = await self._load(source, ref, self.settings.mode)
            active = await self._active_sha()
            if result.sha and result.sha == active:
                return None
            if result.sha and result.sha == self._seen and result.errors:
                return None
            result = await self._with_protection(result)
            activated = await self.store.save(result)
            if result.errors:
                self._seen = result.sha
                log.warning(
                    "definition %s rejected (%s): %s",
                    result.sha[:12],
                    reason,
                    "; ".join(str(e) for e in result.errors)[:500],
                )
                return None
            self._seen = None
            return result.sha if activated else None

    async def _with_protection(self, result: LoadResult) -> LoadResult:
        repo = self.repo
        if (
            repo is None
            or self.settings.mode is not FactoryMode.SERVER
            or result.errors
            or not result.sha
        ):
            return result
        try:
            rules = await self._client(repo).branch_rules(self.branch)
            ok = definition_protected(rules)
        except Exception as exc:  # cannot prove protection: do not activate
            log.warning("definition repo ruleset unreadable: %r", exc)
            ok = False
        if ok:
            return result
        err = DefinitionError("", None, "repository", UNPROTECTED_DEFINITION)
        return LoadResult(result.sha, result.source, None, (err,))

    async def run_poll_loop(self, interval_s: int = 300) -> None:
        """Sync at start (so a fresh instance is ready at once), then every ``interval_s``."""
        reason = "startup"
        while True:
            try:
                await self.sync_now(reason=reason)
            except Exception as exc:
                log.warning("definition sync failed (%s): %r", reason, exc)
            reason = "poll"
            await asyncio.sleep(interval_s)

    async def handle(self, payload: Mapping[str, Any]) -> None:
        """One notification: a pull_request gets the check on its head; anything else a sync."""
        head = payload.get("head_sha")
        repo = self.repo
        if payload.get("reason") == "pull_request" and head and repo:
            await self.check_pr(repo, str(head))
        else:
            await self.sync_now(reason=str(payload.get("reason") or "notify"))

    async def listen(self) -> None:
        """LISTEN definition_sync and run `handle` for each notification; reconnects on loss."""
        import psycopg

        dsn = self._dsn or self.settings.libpq_dsn()
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    async for note in conn.notifies():
                        try:
                            data = json.loads(note.payload) if note.payload else {}
                        except ValueError:
                            data = {}
                        try:
                            await self.handle(data if isinstance(data, dict) else {})
                        except Exception as exc:
                            log.warning("definition sync notification failed: %r", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("definition_sync LISTEN lost: %r", exc)
            await asyncio.sleep(5)

    # -- the PR check ------------------------------------------------------------

    async def check_pr(self, repo: str, head_sha: str) -> str:
        """Load the PR head (objects only) and publish ``factory/definition``; returns the
        conclusion: ``success``, ``failure`` or ``skipped`` (a PAT cannot use the Checks API)."""
        if self.auth is None or isinstance(self.auth, PatAuth):
            if not self._warned_checks:
                self._warned_checks = True
                log.warning("skipping %s: a PAT cannot use the Checks API (R09)", DEFINITION_CHECK)
            return "skipped"
        result = await self._load(f"https://github.com/{repo}", head_sha, self.settings.mode)
        if result.errors or result.definition is None:
            errors = [str(e) for e in result.errors] or ["definition did not load"]
            conclusion, title = "failure", "Factory definition is invalid"
            summary = "\n".join(f"- {e}" for e in errors)[:_SUMMARY_LIMIT]
        else:
            conclusion, title = "success", "Factory definition is valid"
            summary = f"Definition {result.sha[:12]} validates."
        await self._client(repo).upsert_check_run(
            head_sha=head_sha,
            name=DEFINITION_CHECK,
            conclusion=conclusion,
            title=title,
            summary=summary,
            details_url=f"https://github.com/{repo}/commit/{head_sha}",
        )
        return conclusion
