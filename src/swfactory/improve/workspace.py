"""The improver's definition workspace: seed a sandbox, read its diff back, commit, push a branch.

The proposal is untrusted. The sandbox is the agent's, so its git history is never read: only the
final file contents come back (plain bytes), what changed is computed on the host against the
tree the host itself checked out, and the host commit is rebuilt from that text on a clean clone.
No credentialed git runs here: GitHub is reached through gitops containers (bundles), the local
source through a plain hardened clone."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID

from swfactory.definition.loader import defaults_dir
from swfactory.definition.sync import definition_repo_slug
from swfactory.domain.types import Role
from swfactory.github.auth import PERMS_DEFINITION_PR, PERMS_FETCH, PERMS_PUSH, GitHubAuth
from swfactory.github.client import GitHubClient, MutationQueue
from swfactory.gitops.github import (
    GITOPS_XFER,
    GitopsRunner,
    git_argv,
    hardened_git_args,
)
from swfactory.improve.proposal import FileChange
from swfactory.sandbox.base import Sandbox
from swfactory.sandbox.paths import IN, REPO
from swfactory.sandbox.template import HARDENED_GIT_ENV

__all__ = [
    "DefinitionWorkspace",
    "WorkspaceError",
    "improve_branch",
    "read_changes",
    "rebuild_changes",
    "seed_sandbox",
]

BOT_NAME = "factory-bot"
BOT_EMAIL = "factory-bot@users.noreply.github.com"
SEED_WHEN = "2000-01-01T00:00:00+00:00"
MAX_CHANGES = 200
MAX_TEXT_BYTES = 2 * 2**20
MAX_TOTAL_BYTES = 8 * 2**20
MAX_BUNDLE_BYTES = 512 * 2**20
_BRANCH = re.compile(r"^improve/[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SANDBOX_GIT = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.excludesFile=/dev/null",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.quotepath=off",
    "-c",
    "diff.external=",
    "-c",
    "core.pager=cat",
    "-c",
    "commit.gpgsign=false",
)
_BOT_ENV = {
    "GIT_AUTHOR_NAME": BOT_NAME,
    "GIT_AUTHOR_EMAIL": BOT_EMAIL,
    "GIT_COMMITTER_NAME": BOT_NAME,
    "GIT_COMMITTER_EMAIL": BOT_EMAIL,
}


class WorkspaceError(Exception):
    pass


def improve_branch(day: date, role: Role, key: UUID | str | None = None) -> str:
    """`improve/<date>-<role>`, plus a short suffix from the improvement id when one is given.

    The suffix is a pure function of the id, so a crash-replay of the same improvement lands on
    the same branch while a re-run on the same day (after a closed or merged PR) gets a new one."""
    base = f"improve/{day:%Y%m%d}-{role.value}"
    if key is None:
        return base
    return f"{base}-{str(key).replace('-', '')[:8]}"


def _in_benchmarks(path: str) -> bool:
    return path == "benchmarks" or path.startswith("benchmarks/")


def _sb_git(*args: str) -> list[str]:
    return ["git", *_SANDBOX_GIT, *args]


async def _sb_run(box: Sandbox, *args: str, env: Mapping[str, str] | None = None) -> str:
    r = await box.exec(_sb_git(*args), cwd=REPO, env=env, timeout_s=120)
    if r.exit_code != 0 or r.timed_out:
        raise WorkspaceError(f"git {args[0]} failed in the sandbox: {r.stderr.strip()[:300]}")
    return r.stdout


async def seed_sandbox(box: Sandbox, files: Mapping[str, bytes]) -> str:
    """Write the definition tree (never `benchmarks/`) into REPO as a fresh repo; return its sha."""
    for rel, data in sorted(files.items()):
        if _in_benchmarks(rel):
            continue
        await box.write_bytes(f"{REPO}/{rel}", data)
    roles = defaults_dir() / "roles"
    if roles.is_dir():
        for p in sorted(roles.glob("*.md")):
            await box.write_bytes(f"{IN}/shipped-roles/{p.name}", p.read_bytes())
    env = {
        **_BOT_ENV,
        "GIT_AUTHOR_DATE": SEED_WHEN,
        "GIT_COMMITTER_DATE": SEED_WHEN,
    }
    await _sb_run(box, "init", "-q", "-b", "main")
    await _sb_run(box, "add", "-A", "-f")
    await _sb_run(box, "commit", "-q", "--allow-empty", "--no-verify", "-m", "base", env=env)
    return (await _sb_run(box, "rev-parse", "HEAD")).strip()


def _split(raw: str) -> list[str]:
    parts = raw.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    return parts


MAX_SANDBOX_FILES = 2000


async def _find(box: Sandbox, *expr: str) -> list[str]:
    """Relative paths under REPO (never inside a `.git`) matching a `find` expression."""
    argv = ["find", ".", "-name", ".git", "-prune", "-o", *expr, "-print0"]
    r = await box.exec(argv, cwd=REPO, timeout_s=120)
    if r.exit_code != 0 or r.timed_out:
        raise WorkspaceError(f"cannot list the sandbox repository: {r.stderr.strip()[:300]}")
    out = []
    for p in _split(r.stdout):
        if "\ufffd" in p:
            raise WorkspaceError("a changed path is not valid utf-8")
        out.append(p[2:] if p.startswith("./") else p)
    return out


def _decode(data: bytes, what: str) -> str:
    if b"\0" in data:
        raise WorkspaceError(f"{what} is binary or not utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise WorkspaceError(f"{what} is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkspaceError(f"{what} is binary or not utf-8") from None


async def read_changes(box: Sandbox, base_files: Mapping[str, bytes]) -> list[FileChange]:
    """What the sandbox tree looks like now against the HOST's copy of the base (fail closed).

    The sandbox is the agent's: its git history, index, HEAD and objects are never trusted, so
    only the final file contents are read from it (as plain bytes). Status and old text always
    come from `base_files`, the tree the host checked out itself, so an agent that commits a
    forbidden edit and then makes a small one still shows the whole difference from the base."""
    if (await box.exec(_sb_git("rev-parse", "--git-dir"), cwd=REPO, timeout_s=60)).exit_code != 0:
        raise WorkspaceError("the sandbox repository is unreadable")  # fail closed
    present = await _find(box, "-type", "f")
    links = await _find(box, "-type", "l")
    odd = await _find(box, "!", "-type", "f", "!", "-type", "l", "!", "-type", "d")
    if odd:
        raise WorkspaceError("the sandbox repository holds a device, socket or pipe")
    execs = set(await _find(box, "-type", "f", "-perm", "-100"))
    big = await _find(box, "-type", "f", "-size", f"+{MAX_TEXT_BYTES}c")
    if len(present) + len(links) > MAX_SANDBOX_FILES:
        raise WorkspaceError("the proposal is too large")
    base = {p: d for p, d in base_files.items() if not _in_benchmarks(p)}
    out: list[FileChange] = []
    total = 0

    def add(change: FileChange) -> None:
        nonlocal total
        total += len((change.old_text or "").encode()) + len((change.new_text or "").encode())
        if len(out) + 1 > MAX_CHANGES or total > MAX_TOTAL_BYTES:
            raise WorkspaceError("the proposal is too large")
        out.append(change)

    paths = sorted({*present, *links, *base})
    kinds = {**dict.fromkeys(present, "f"), **dict.fromkeys(links, "l")}
    for path in paths:
        if _in_benchmarks(path):
            continue  # never seeded; a re-created benchmarks/ is the agent's own and not ours
        kind = kinds.get(path)
        old_bytes = base.get(path)
        if kind is None:
            if old_bytes is not None:
                add(FileChange(path, "D", None, "000000", _decode(old_bytes, path), None))
            continue
        if kind == "l":
            add(FileChange(path, "M" if old_bytes is not None else "A", None, "120000", None, None))
            continue
        if path in big:
            raise WorkspaceError("a changed file is too large")
        data = await box.read_bytes(f"{REPO}/{path}")
        if data is None:
            raise WorkspaceError("a sandbox file vanished while it was read")
        mode = "100755" if path in execs else "100644"
        if old_bytes is not None and data == old_bytes and mode == "100644":
            continue
        new_text = _decode(data, "a changed file")
        old_text = _decode(old_bytes, path) if old_bytes is not None else None
        add(FileChange(path, "M" if old_bytes is not None else "A", None, mode, old_text, new_text))
    return out


def rebuild_changes(
    changes: Sequence[FileChange], base_files: Mapping[str, bytes]
) -> list[FileChange]:
    """Re-derive status and old text on the host: only path, mode and new text are kept.

    Whatever a stored change says about the old side is dropped, so every check runs on the
    difference between the real base and the proposed text."""
    out: list[FileChange] = []

    def old_of(path: str) -> str | None:
        data = base_files.get(path)
        return None if data is None else _decode(data, path)

    for c in changes:
        if c.status == "R" and c.old_path is not None:
            out.append(FileChange(c.old_path, "D", None, "000000", old_of(c.old_path), None))
        if c.status == "D":
            out.append(FileChange(c.path, "D", None, "000000", old_of(c.path), None))
            continue
        old = old_of(c.path)
        out.append(
            FileChange(c.path, "M" if old is not None else "A", None, c.mode, old, c.new_text)
        )
    return out


def _host_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **HARDENED_GIT_ENV}
    env["HOME"] = tempfile.gettempdir()
    env["GIT_ALLOW_PROTOCOL"] = "file"
    env.update(extra or {})
    return env


def _git(
    cwd: Path | None,
    *args: str,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> str:
    argv = ["git", *hardened_git_args(), "-c", "protocol.file.allow=always"]
    argv += ["-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args]
    p = subprocess.run(
        argv,
        cwd=cwd,
        env=_host_env(env),
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if p.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def _safe_rel(path: str) -> PurePosixPath:
    p = PurePosixPath(path)
    bad = (
        not path
        or "\0" in path
        or "\\" in path
        or path.startswith("/")
        or any(part in ("", ".", "..") or part.lower() == ".git" for part in p.parts)
    )
    if bad:
        raise WorkspaceError(f"unsafe path {path!r}")
    return p


class DefinitionWorkspace:
    def __init__(
        self,
        source: str,
        root: Path,
        *,
        job_id: UUID,
        auth: GitHubAuth | None,
        gitops: GitopsRunner | None,
        client: GitHubClient | None = None,
        branch: str = "main",
    ) -> None:
        self.source = source
        self.root = Path(root)
        self.job_id = job_id
        self.auth = auth
        self.gitops = gitops
        self.branch = branch
        self._client = client
        self.slug = definition_repo_slug(source)
        self.path = self.root / str(job_id)
        self._base: str | None = None

    # -- checkout -------------------------------------------------------------
    async def checkout(self, base_sha: str) -> dict[str, bytes]:
        if not _SHA.match(base_sha):
            raise WorkspaceError("base sha must be 40 hex characters")
        await asyncio.to_thread(self._reset_dir)
        if self.slug is None:
            origin = Path(self.source)
            if not origin.is_dir():
                raise WorkspaceError("definition source is not a directory")
            await asyncio.to_thread(
                _git, None, "clone", "--no-checkout", "--", str(origin), str(self.path)
            )
        else:
            bundle = await self._fetch_bundle()
            try:
                await asyncio.to_thread(
                    _git, None, "clone", "--no-checkout", "--", str(bundle), str(self.path)
                )
            finally:
                bundle.unlink(missing_ok=True)
        await asyncio.to_thread(self._checkout_base, base_sha)
        self._base = base_sha
        return await asyncio.to_thread(self._read_tree)

    def _reset_dir(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def _checkout_base(self, base_sha: str) -> None:
        _git(self.path, "checkout", "-q", "--detach", base_sha, "--")
        head = _git(self.path, "rev-parse", "HEAD").strip()
        if head != base_sha:
            raise WorkspaceError("checkout did not land on the base commit")

    def _read_tree(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for dirpath, dirs, names in os.walk(self.path):
            if Path(dirpath) == self.path and ".git" in dirs:
                dirs.remove(".git")
            for n in names:
                p = Path(dirpath) / n
                if p.is_symlink() or not p.is_file():
                    continue
                out[p.relative_to(self.path).as_posix()] = p.read_bytes()
        return out

    async def _fetch_bundle(self) -> Path:
        gitops, auth, slug = self.gitops, self.auth, self.slug
        if gitops is None or auth is None or slug is None:
            raise WorkspaceError("a GitHub definition source needs credentials and gitops")
        token = await auth.token_for(slug, PERMS_FETCH)
        for op, argv, tok in (
            ("init", git_argv("init", repo=slug), None),
            ("fetch", git_argv("fetch", repo=slug, branch=self.branch), token),
            ("export_bundle", git_argv("export_bundle", repo=slug, branch=self.branch), None),
        ):
            r = await gitops.run(self.job_id, argv, token=tok)
            if r.exit_code != 0:
                raise WorkspaceError(f"gitops {op} failed: {r.stderr.strip()[:300]}")
        fd, name = tempfile.mkstemp(prefix="improve-base-", suffix=".bundle")
        os.close(fd)
        dest = Path(name)
        try:
            await gitops.get_file(
                self.job_id, f"{GITOPS_XFER}/base.bundle", dest, max_bytes=MAX_BUNDLE_BYTES
            )
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return dest

    # -- commit ---------------------------------------------------------------
    async def commit(
        self, branch: str, changes: Sequence[FileChange], *, message: str, when: datetime
    ) -> str:
        if self._base is None:
            raise WorkspaceError("checkout first")
        self._check_branch(branch)
        return await asyncio.to_thread(self._commit_sync, branch, list(changes), message, when)

    @staticmethod
    def _check_branch(branch: str) -> None:
        if not _BRANCH.match(branch) or ".." in branch or branch.endswith((".lock", ".")):
            raise WorkspaceError(f"branch {branch!r} is not an improver branch")

    def _target(self, rel: str) -> Path:
        p = _safe_rel(rel)
        cur = self.path
        for part in p.parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                raise WorkspaceError(f"{rel!r} passes through a symlink")
        target = cur / p.parts[-1]
        if target.is_symlink():
            raise WorkspaceError(f"{rel!r} is a symlink")
        return target

    def _commit_sync(
        self, branch: str, changes: list[FileChange], message: str, when: datetime
    ) -> str:
        assert self._base is not None
        _git(self.path, "checkout", "-q", "-f", "-B", branch, self._base, "--")
        _git(self.path, "clean", "-fdq")
        touched: list[str] = []
        for c in changes:
            if _in_benchmarks(c.path) or (c.old_path and _in_benchmarks(c.old_path)):
                raise WorkspaceError("benchmarks/ cannot be changed")
            if c.status == "D":
                self._target(c.path).unlink(missing_ok=True)
                touched.append(c.path)
                continue
            if c.mode != "100644" or c.new_text is None:
                raise WorkspaceError(f"{c.path!r} is not a plain text file")
            if c.status == "R" and c.old_path:
                self._target(c.old_path).unlink(missing_ok=True)
                touched.append(c.old_path)
            target = self._target(c.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(c.new_text.encode())
            touched.append(c.path)
        if not touched:
            raise WorkspaceError("nothing to commit")
        _git(self.path, "add", "-A", "--", *touched)
        stamp = (when if when.tzinfo else when.replace(tzinfo=UTC)).isoformat()
        env = {**_BOT_ENV, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        _git(self.path, "commit", "-q", "--no-verify", "-F", "-", env=env, stdin=message)
        return _git(self.path, "rev-parse", "HEAD").strip()

    # -- push -----------------------------------------------------------------
    async def push(self, branch: str, sha: str) -> None:
        self._check_branch(branch)
        if not _SHA.match(sha):
            raise WorkspaceError("sha must be 40 hex characters")
        if self.slug is None:
            await asyncio.to_thread(self._push_local, branch, sha)
        else:
            await self._push_github(branch, sha)

    def _push_local(self, branch: str, sha: str) -> None:
        origin = Path(self.source)
        if not origin.is_dir() or not os.access(origin, os.W_OK):
            raise WorkspaceError("definition repo is read-only")
        try:
            _git(origin, "rev-parse", "--git-dir")
        except WorkspaceError:
            raise WorkspaceError("definition repo is read-only") from None
        ref = f"refs/heads/{branch}"
        remote = _git(self.path, "ls-remote", "origin", ref).split()
        if remote and remote[0] == sha:
            return
        if remote:
            raise WorkspaceError("branch changed on GitHub")
        _git(self.path, "push", "-q", "origin", f"{sha}:{ref}")

    async def _push_github(self, branch: str, sha: str) -> None:
        gitops, auth, slug = self.gitops, self.auth, self.slug
        if gitops is None or auth is None or slug is None:
            raise WorkspaceError("a GitHub definition source needs credentials and gitops")
        client = self._client or GitHubClient(
            auth, slug, MutationQueue(), permissions=PERMS_DEFINITION_PR
        )
        live = await client.branch_sha(branch)
        if live == sha:
            return
        if live is not None:
            raise WorkspaceError("branch changed on GitHub")
        fd, name = tempfile.mkstemp(prefix="improve-job-", suffix=".bundle")
        os.close(fd)
        bundle = Path(name)
        try:
            await asyncio.to_thread(
                _git, self.path, "bundle", "create", str(bundle), f"refs/heads/{branch}"
            )
            await gitops.put_file(self.job_id, f"{GITOPS_XFER}/job.bundle", bundle)
        finally:
            bundle.unlink(missing_ok=True)
        token = await auth.token_for(slug, PERMS_PUSH)
        for op, argv, tok in (
            ("import_bundle", git_argv("import_bundle", repo=slug, branch=branch), None),
            ("push", git_argv("push", repo=slug, branch=branch, expect_sha=""), token),
        ):
            r = await gitops.run(self.job_id, argv, token=tok)
            if r.exit_code != 0:
                raise WorkspaceError(f"gitops {op} failed: {r.stderr.strip()[:300]}")
