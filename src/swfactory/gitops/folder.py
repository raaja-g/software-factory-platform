"""A local folder that is the source of truth for an app (the bare repo mirrors its main)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from swfactory.gitops.service import (
    REASON_FOLDER_DIRTY,
    REASON_FOLDER_MOVED,
    SAFE_GIT_ENV,
    app_slug,
    safe_git,
)

if TYPE_CHECKING:
    from swfactory.definition.models import AppDef
    from swfactory.settings import Settings

log = logging.getLogger(__name__)

HOST_REPOS_MOUNT = "/host-repos"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "factory-bot",
    "GIT_AUTHOR_EMAIL": "factory-bot@localhost",
    "GIT_COMMITTER_NAME": "factory-bot",
    "GIT_COMMITTER_EMAIL": "factory-bot@localhost",
}


class FolderError(RuntimeError): ...


class FolderOutsideRoot(FolderError): ...


def _norm(path: str) -> tuple[str, ...] | None:
    if not path.startswith("/"):
        return None
    parts = PurePosixPath(path).parts
    if ".." in parts or "." in parts:
        return None
    return parts


def container_path(local_path: str, host_root: str, mount: str = HOST_REPOS_MOUNT) -> Path:
    """Map a host path under `host_root` to the same place under `mount`."""
    p = _norm(local_path)
    r = _norm(host_root)
    if p is None or r is None or p[: len(r)] != r:
        raise FolderOutsideRoot(f"{local_path} is not inside {host_root}")
    return Path(mount).joinpath(*p[len(r) :])


def _env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")}
    env.update(SAFE_GIT_ENV)
    env.update(_IDENTITY)
    return env


class FolderRepo:
    def __init__(
        self,
        host_root: str,
        mount: str = HOST_REPOS_MOUNT,
        *,
        bare_dir: Path,
        timeout_s: int = 900,
    ) -> None:
        self.host_root = host_root
        self.mount = mount
        self.bare_dir = Path(bare_dir)
        self.timeout_s = timeout_s

    @classmethod
    def from_settings(cls, s: Settings) -> FolderRepo | None:
        if not s.host_repos_root:
            return None
        return cls(s.host_repos_root, bare_dir=Path(s.repos_dir), timeout_s=s.gitops_timeout_s)

    # -- helpers ---------------------------------------------------------

    def _folder(self, local_path: str) -> Path:
        path = container_path(local_path, self.host_root, self.mount)
        real = Path(os.path.realpath(path))
        mount = Path(os.path.realpath(self.mount))
        if real != mount and mount not in real.parents:
            raise FolderOutsideRoot(f"{local_path} resolves outside {self.host_root}")
        return path

    def _app_folder(self, app: AppDef) -> Path:
        if app.local_path is None:
            raise FolderError(f"{app_slug(app)} has no local_path")
        return self._folder(app.local_path)

    def _bare(self, app_or_slug: AppDef | str) -> Path:
        slug = app_or_slug if isinstance(app_or_slug, str) else app_slug(app_or_slug)
        return self.bare_dir / f"{slug}.git"

    async def _run(
        self, args: Sequence[str], cwd: Path, *, folder: Path | None = None
    ) -> tuple[int, str, str]:
        """Run safe git in `cwd`; `folder` is the foreign-owned repo that must be trusted."""
        cmd = safe_git(*args)
        if folder is not None:
            cmd = [*cmd[:1], "-c", f"safe.directory={folder}", *cmd[1:]]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "", f"git {args[0]} timed out after {self.timeout_s}s"
        return proc.returncode if proc.returncode is not None else -1, out.decode(), err.decode()

    async def _ok(self, args: Sequence[str], cwd: Path, *, folder: Path | None = None) -> str:
        rc, out, err = await self._run(args, cwd, folder=folder)
        if rc != 0:
            raise FolderError(f"git {args[0]} failed: {err.strip() or out.strip()}")
        return out.strip()

    async def _fgit(self, folder: Path, *args: str) -> tuple[int, str, str]:
        return await self._run(["-C", str(folder), *args], folder, folder=folder)

    async def _current_branch(self, folder: Path) -> str:
        rc, out, _ = await self._fgit(folder, "symbolic-ref", "-q", "--short", "HEAD")
        return out.strip() if rc == 0 else ""

    async def _default(self, app: AppDef) -> str:
        return await self.detect_default_branch(self._app_folder(app))

    async def default_branch(self, app: AppDef) -> str:
        return await self._default(app)

    # -- public ----------------------------------------------------------

    async def detect_default_branch(self, folder: Path) -> str:
        folder = Path(folder)
        rc, out, _ = await self._fgit(folder, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
        if rc == 0 and out.strip().startswith("refs/remotes/origin/"):
            return out.strip().removeprefix("refs/remotes/origin/")
        for name in ("main", "master"):
            rc, _, _ = await self._fgit(folder, "rev-parse", "--verify", "-q", f"refs/heads/{name}")
            if rc == 0:
                return name
        current = await self._current_branch(folder)
        if not current:
            raise FolderError(f"cannot tell the default branch of {folder}")
        return current

    async def adopt(self, slug: str, local_path: str) -> str:
        folder = self._folder(local_path)
        if not folder.is_dir():
            raise FolderError(f"{local_path} does not exist")
        if not (folder / ".git").exists():
            if any(folder.iterdir()):
                raise FolderError(f"{local_path} is not a git repository")
            await self._ok(["init", "-b", "main", str(folder)], folder, folder=folder)
        rc, _, _ = await self._fgit(folder, "rev-parse", "--verify", "-q", "HEAD")
        if rc != 0:
            await self._ok(
                ["-C", str(folder), "symbolic-ref", "HEAD", "refs/heads/main"],
                folder,
                folder=folder,
            )
            await self._ok(
                [
                    "-C",
                    str(folder),
                    "commit",
                    "--allow-empty",
                    "-q",
                    "-m",
                    "factory: initial commit",
                ],
                folder,
                folder=folder,
            )
        default = await self.detect_default_branch(folder)
        await self._ensure_bare(slug, folder, default)
        return default

    async def _ensure_bare(self, slug: str, folder: Path, default: str) -> Path:
        bare = self._bare(slug)
        if bare.exists():
            return bare
        self.bare_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self._ok(
                ["clone", "--bare", "--no-local", str(folder), str(bare)],
                self.bare_dir,
                folder=folder,
            )
            await self._run(["remote", "remove", "origin"], bare)
            await self._ok(["symbolic-ref", "HEAD", f"refs/heads/{default}"], bare)
        except BaseException:
            shutil.rmtree(bare, ignore_errors=True)
            raise
        return bare

    async def ensure_bare(self, app: AppDef) -> Path:
        folder = self._app_folder(app)
        return await self._ensure_bare(app_slug(app), folder, await self._default(app))

    async def fetch_default(self, app: AppDef) -> str:
        folder = self._app_folder(app)
        bare = await self.ensure_bare(app)
        default = await self._default(app)
        await self._ok(
            ["fetch", "--no-tags", str(folder), f"+refs/heads/{default}:refs/heads/{default}"],
            bare,
            folder=folder,
        )
        return await self._ok(["rev-parse", f"refs/heads/{default}"], bare)

    async def check_clean(self, app: AppDef) -> str | None:
        folder = self._app_folder(app)
        default = await self._default(app)
        if await self._current_branch(folder) != default:
            return None
        out = await self._ok(["-C", str(folder), "status", "--porcelain"], folder, folder=folder)
        return REASON_FOLDER_DIRTY if out else None

    async def update_folder(self, app: AppDef, *, branch: str, merge_sha: str) -> str | None:
        reason = await self.check_clean(app)
        if reason:
            return reason
        folder = self._app_folder(app)
        bare = self._bare(app)
        default = await self._default(app)
        if await self._current_branch(folder) == default:
            incoming = f"refs/factory/incoming/{default}"
            await self._ok(
                [
                    "-C",
                    str(folder),
                    "fetch",
                    "--no-tags",
                    str(bare),
                    f"+refs/heads/{default}:{incoming}",
                ],
                folder,
                folder=folder,
            )
            got = (await self._fgit(folder, "rev-parse", incoming))[1].strip()
            if got != merge_sha:
                return REASON_FOLDER_MOVED
            rc, _, _ = await self._fgit(folder, "merge", "--ff-only", merge_sha)
            if rc != 0:
                return REASON_FOLDER_MOVED
        else:
            rc, _, _ = await self._fgit(
                folder,
                "fetch",
                "--no-tags",
                str(bare),
                f"refs/heads/{default}:refs/heads/{default}",
            )
            if rc != 0:
                return REASON_FOLDER_MOVED
        rc, _, err = await self._fgit(
            folder, "fetch", "--no-tags", str(bare), f"+refs/heads/{branch}:refs/heads/{branch}"
        )
        if rc != 0:
            log.warning(
                "could not keep %s in the folder of %s: %s", branch, app_slug(app), err.strip()
            )
        return None
