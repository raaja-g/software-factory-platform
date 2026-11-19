"""First-use registration: write `apps/<slug>.yaml` for a repo the person points at.

Only the orchestrator runs this (it mounts /host-repos); the api never calls it.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.definition.models import AppDef, Definition
from swfactory.definition.store import load_active
from swfactory.definition.writer import DefinitionWriteError, DefinitionWriter
from swfactory.events import record_event
from swfactory.github.client import GitHubClient, GitHubError, MutationQueue
from swfactory.gitops.folder import FolderError, FolderRepo

if TYPE_CHECKING:
    from swfactory.github.auth import GitHubAuth
    from swfactory.settings import Settings

_GH_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_GH_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# first segments that are far more likely a relative folder than a GitHub owner
_FOLDER_WORDS = frozenset(
    {"work", "src", "home", "tmp", "users", "projects", "repos", "code", "dev", "var", "opt", "usr"}
)
_SLUG_MAX = 40
_WRITE_TRIES = 3
_RETRY_DELAY_S = 1.0
_USAGE = "use an absolute path, a ~ path, owner/name or a GitHub URL"


class RegisterError(ValueError):
    """Why a repo cannot be registered; the message is shown to the person."""


class RegistrationPending(RuntimeError):
    """The registration was proposed as a pull request and waits for its merge."""

    def __init__(self, slug: str, pr_url: str | None) -> None:
        super().__init__(f"registration of {slug} waits for {pr_url}")
        self.slug = slug
        self.pr_url = pr_url


@dataclass(frozen=True, slots=True)
class RepoRef:
    kind: Literal["local", "github"]
    local_path: str | None
    full_name: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class RepoInfo:
    reachable: bool
    default_branch: str | None
    empty: bool


class RepoInspector(Protocol):
    async def inspect(self, full_name: str) -> RepoInfo: ...


@dataclass
class RegisterDeps:
    writer: DefinitionWriter
    folders: FolderRepo | None
    settings: Settings
    inspector: RepoInspector | None
    sync: Callable[[AsyncSession], Awaitable[Definition]]

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        writer: DefinitionWriter,
        sync: Callable[[AsyncSession], Awaitable[Definition]],
        inspector: RepoInspector | None = None,
    ) -> RegisterDeps:
        return cls(writer, FolderRepo.from_settings(settings), settings, inspector, sync)


class GitHubRepoInspector:
    """Reads a GitHub repo's reachability, default branch and emptiness (read calls only)."""

    def __init__(self, auth: GitHubAuth) -> None:
        self._auth = auth
        self._queue = MutationQueue()

    async def inspect(self, full_name: str) -> RepoInfo:
        client = GitHubClient(self._auth, full_name, self._queue)
        try:
            if not await client.installation_reachable():
                return RepoInfo(False, None, False)
            data = await client.get_repo()
            if data is None:
                return RepoInfo(False, None, False)
            branch = str(data.get("default_branch") or "") or None
            empty = False
            if branch:
                try:
                    empty = await client.branch_sha(branch) is None
                except GitHubError as exc:
                    if exc.status != 409:  # 409: "Git Repository is empty"
                        raise
                    empty = True
            return RepoInfo(True, branch, empty)
        finally:
            await client.aclose()


def _github(owner: str, name: str) -> RepoRef:
    if (
        not _GH_NAME.match(owner)
        or not _GH_NAME.match(name)
        or set(owner) == {"."}
        or set(name) == {"."}
    ):
        raise RegisterError(f"'{owner}/{name}' is not a GitHub repo name; {_USAGE}")
    return RepoRef("github", None, f"{owner}/{name}", f"https://github.com/{owner}/{name}")


def parse_repo(text: str, *, host_home: str | None) -> RepoRef:
    if _CONTROL.search(text):
        raise RegisterError("the repo contains control characters")
    text = text.strip()
    if not text:
        raise RegisterError(f"no repo given; {_USAGE}")
    if text.startswith("/") or text.startswith("~"):
        raw = text
        if text.startswith("~"):
            if not text.startswith("~/"):
                raise RegisterError(f"'{text}': ~user paths are not supported; {_USAGE}")
            if not host_home:
                raise RegisterError("a ~ path needs FACTORY_HOST_HOME to be set")
            raw = host_home.rstrip("/") + text[1:]
        if ".." in raw.split("/"):
            raise RegisterError(f"'{text}' must not contain '..'")
        return RepoRef("local", posixpath.normpath(raw), None, None)
    m = _GH_URL.match(text)
    if m:
        return _github(m.group(1), m.group(2))
    if "://" not in text and text.count("/") == 1:
        owner, name = text.split("/")
        if owner.lower() not in _FOLDER_WORDS:
            return _github(owner, name.removesuffix(".git"))
    raise RegisterError(f"cannot use '{text}'; {_USAGE}")


def slug_for(name: str, taken: Collection[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower().rstrip("/").rsplit("/", 1)[-1]).strip("-")
    base = base[:_SLUG_MAX].strip("-") or "app"
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        cand = base[: _SLUG_MAX - len(suffix)].rstrip("-") + suffix
        if cand not in taken:
            return cand
        n += 1


def _gh_key(url: str) -> str:
    return url.removesuffix("/").removesuffix(".git").removesuffix("/").lower()


def find_registered(defn: Definition, ref: RepoRef) -> tuple[str, AppDef] | None:
    for slug, app in defn.factory.apps.items():
        if ref.kind == "local":
            if app.repo_kind == "local" and app.local_path == ref.local_path:
                return slug, app
        elif app.repo_kind == "github" and ref.url and _gh_key(app.repo_url) == _gh_key(ref.url):
            return slug, app
    return None


async def _adopt(deps: RegisterDeps, ref: RepoRef, slug: str, ci_safe: bool) -> str:
    root = deps.settings.host_repos_root
    if not root:
        raise RegisterError("local folders need FACTORY_HOST_REPOS_ROOT")
    path = ref.local_path or ""
    base = root.rstrip("/")
    if path != base and not path.startswith(base + "/"):
        raise RegisterError(f"'{path}' is not inside FACTORY_HOST_REPOS_ROOT")
    if ci_safe:
        raise RegisterError("--ci-safe applies to GitHub repos only")
    if deps.folders is None:
        raise RegisterError("local folders need FACTORY_HOST_REPOS_ROOT")
    try:
        return await deps.folders.adopt(slug, path)
    except FolderError as exc:
        raise RegisterError(str(exc)) from exc


async def register_repo(
    s: AsyncSession,
    *,
    repo: str,
    ci_safe: bool = False,
    requested_by: str,
    deps: RegisterDeps | None = None,
) -> AppDef:
    if deps is None:
        raise RegisterError("registration is not available here")
    ref = parse_repo(repo, host_home=deps.settings.host_home)
    active = await load_active(s)
    if active is None:
        raise RegisterError("no definition is active yet")
    defn = active[1]
    found = find_registered(defn, ref)
    if found is not None:
        return found[1]
    name = ref.local_path if ref.kind == "local" else ref.full_name
    slug = slug_for(name or "", set(defn.factory.apps))
    body: dict[str, Any]
    if ref.kind == "local":
        branch = await _adopt(deps, ref, slug, ci_safe)
        body = {
            "repo_kind": "local",
            "repo_url": slug,
            "local_path": ref.local_path,
            "default_branch": branch,
            "merge_policy": "local_merge",
        }
    else:
        if deps.inspector is None:
            raise RegisterError("GitHub repos need the GitHub App (not set up yet)")
        info = await deps.inspector.inspect(ref.full_name or "")
        if not info.reachable:
            raise RegisterError(f"add {ref.full_name} to the app installation")
        if info.empty:
            # creating the first commit would push to the repo before anyone approved
            raise RegisterError(
                f"{ref.full_name} has no commits; create the first commit and run again"
            )
        body = {
            "repo_kind": "github",
            "repo_url": ref.url,
            "default_branch": info.default_branch or "main",
            "merge_policy": "pr_auto_merge",
        }
    body |= {"profile": defn.factory.defaults.profile, "runner": "standard"}
    if ref.kind == "github":
        body["github"] = {"ci_safe": ci_safe}
    AppDef.model_validate(body)
    content = yaml.safe_dump(body, sort_keys=False)
    result = None
    for attempt in range(_WRITE_TRIES):
        try:
            result = await deps.writer.save_file(
                f"apps/{slug}.yaml", content, message=f"app: register {slug}", author=requested_by
            )
            break
        except DefinitionWriteError as exc:
            if attempt == _WRITE_TRIES - 1:
                raise RegisterError(f"could not save apps/{slug}.yaml: {exc}") from exc
            await asyncio.sleep(_RETRY_DELAY_S)
    assert result is not None
    if result.mode == "pr":
        raise RegistrationPending(slug, result.pr_url)
    synced = await deps.sync(s)
    await record_event(
        s, None, "admin.changed", {"what": "app.register", "target": slug, "by": requested_by}
    )
    return synced.factory.apps[slug]
