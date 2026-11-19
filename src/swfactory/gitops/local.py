"""Git service for local repos: a bare repo per app, refs travel as bundles."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from swfactory.definition.models import MergePolicy
from swfactory.gitops.folder import FolderError, FolderRepo
from swfactory.gitops.service import (
    REASON_FOLDER_MOVED,
    REASON_HEAD_MISMATCH,
    REASON_NOTHING_TO_DELIVER,
    SAFE_GIT_ENV,
    Delivery,
    DiffStat,
    GitService,
    app_slug,
    safe_git,
)
from swfactory.queue import branch_name
from swfactory.sandbox.base import TransferTooLarge
from swfactory.sandbox.paths import REPO

if TYPE_CHECKING:
    from swfactory.db.tables import Job
    from swfactory.definition.models import AppDef
    from swfactory.sandbox.base import Sandbox

GIT_DIR = "/work/.factory/git"
BASE_BUNDLE = f"{GIT_DIR}/base.bundle"
JOB_BUNDLE = f"{GIT_DIR}/job.bundle"
IDENTITY = {
    "GIT_AUTHOR_NAME": "factory-bot",
    "GIT_AUTHOR_EMAIL": "factory-bot@localhost",
    "GIT_COMMITTER_NAME": "factory-bot",
    "GIT_COMMITTER_EMAIL": "factory-bot@localhost",
}


class GitError(RuntimeError): ...


async def _box_git(box: Sandbox, *args: str, cwd: str = REPO):  # noqa: ANN202
    return await box.exec(safe_git(*args), cwd=cwd, env=SAFE_GIT_ENV)


def _host_env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")}
    env.update(SAFE_GIT_ENV)
    env.update(IDENTITY)
    return env


async def _run(args: Sequence[str], cwd: Path) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *safe_git(*args),
        cwd=cwd,
        env=_host_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode if proc.returncode is not None else -1, out.decode(), err.decode()


async def _ok(args: Sequence[str], cwd: Path) -> str:
    rc, out, err = await _run(args, cwd)
    if rc != 0:
        raise GitError(f"git {args[0]} failed: {err.strip() or out.strip()}")
    return out.strip()


async def sandbox_diff_stat(box: Sandbox, base: str) -> DiffStat:
    """Files and lines the sandbox repo's HEAD changed since ``base``."""
    r = await _box_git(
        box,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--numstat",
        "-z",
        "--no-renames",
        f"{base}...HEAD",
    )
    if r.exit_code != 0:
        raise GitError(f"diff failed: {r.stderr.strip()}")
    ins = dels = 0
    paths: list[str] = []
    for rec in r.stdout.split("\0"):
        if not rec:
            continue
        a, d, path = rec.split("\t", 2)
        ins += 0 if a == "-" else int(a)
        dels += 0 if d == "-" else int(d)
        paths.append(path)
    return DiffStat(len(paths), ins, dels, tuple(paths))


class LocalGit(GitService):
    def __init__(
        self, repos_dir: Path, *, max_bundle_mb: int = 2048, folders: FolderRepo | None = None
    ) -> None:
        self.repos_dir = Path(repos_dir)
        self.folders = folders
        self.max_bundle_mb = max_bundle_mb
        self._locks: dict[str, asyncio.Lock] = {}

    # -- helpers ---------------------------------------------------------

    def _bare(self, app: AppDef) -> Path:
        return self.repos_dir / f"{app_slug(app)}.git"

    def _lock(self, app: AppDef) -> asyncio.Lock:
        return self._locks.setdefault(app_slug(app), asyncio.Lock())

    def _spool(self) -> Path:
        d = self.repos_dir / ".spool"
        d.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=d))

    @staticmethod
    async def _box_git(box: Sandbox, *args: str, cwd: str = REPO):  # noqa: ANN205
        return await box.exec(safe_git(*args), cwd=cwd, env=SAFE_GIT_ENV)

    def init_bare(self, slug: str, seed: Path | None = None) -> Path:
        bare = self.repos_dir / f"{slug}.git"
        if bare.exists():
            return bare
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        env = {**_host_env()}

        def git(*args: str, cwd: Path) -> None:
            subprocess.run(
                safe_git(*args), cwd=cwd, env=env, check=True, capture_output=True, text=True
            )

        git("init", "--bare", "-b", "main", str(bare), cwd=self.repos_dir)
        if seed is not None:
            with tempfile.TemporaryDirectory() as td:
                work = Path(td) / "seed"
                shutil.copytree(seed, work, ignore=shutil.ignore_patterns(".git"))
                git("init", "-b", "main", cwd=work)
                git("add", "-A", cwd=work)
                git("commit", "-q", "-m", "initial commit", cwd=work)
                git("push", "-q", str(bare), "main", cwd=work)
        return bare

    async def base_head(self, job: Job, app: AppDef) -> str:
        return await _ok(["rev-parse", f"refs/factory/base/{job.id}"], self._bare(app))

    async def _make_base_bundle(
        self, bare: Path, app: AppDef, spool: Path, *, exclude: str | None, with_branch: str | None
    ) -> Path | None:
        default = app.default_branch
        refs = [f"refs/heads/{default}"]
        if with_branch is not None:
            refs.append(f"refs/heads/{with_branch}")
        args = ["bundle", "create", str(spool / "base.bundle"), *refs]
        if exclude is not None:
            args.append(f"^{exclude}")
        rc, _, _ = await _run(args, bare)
        if rc != 0:
            return None
        return spool / "base.bundle"

    async def _ref_exists(self, bare: Path, ref: str) -> bool:
        rc, _, _ = await _run(["rev-parse", "--verify", "-q", ref], bare)
        return rc == 0

    # -- GitService ------------------------------------------------------

    async def _folder_sync(self, app: AppDef) -> AppDef:
        """For a folder app: refresh the bare mirror and use the folder's default branch."""
        if self.folders is None:
            raise FolderError("FACTORY_HOST_REPOS_ROOT is not set")
        await self.folders.fetch_default(app)
        default = await self.folders.default_branch(app)
        return app.model_copy(update={"default_branch": default})

    async def prepare(self, job: Job, app: AppDef, box: Sandbox) -> str:
        async with self._lock(app):
            if app.local_path is not None:
                if self.folders is None:
                    raise FolderError("FACTORY_HOST_REPOS_ROOT is not set")
                await self.folders.ensure_bare(app)
                app = await self._folder_sync(app)
            return await self._prepare(job, app, box)

    async def _prepare(self, job: Job, app: AppDef, box: Sandbox) -> str:
        bare = self._bare(app)
        default = app.default_branch
        branch = branch_name(job.seq, job.slug)
        has_repo = (await box.read_bytes(f"{REPO}/.git/HEAD")) is not None
        base_ref = f"refs/factory/base/{job.id}"
        if not has_repo:
            kept = await self._ref_exists(bare, f"refs/heads/{branch}")
            spool = self._spool()
            try:
                bundle = await self._make_base_bundle(
                    bare, app, spool, exclude=None, with_branch=branch if kept else None
                )
                if bundle is None:
                    raise GitError(f"cannot bundle {default} of {app_slug(app)}")
                await box.exec(["mkdir", "-p", GIT_DIR], cwd="/work")
                await box.copy_in(bundle, BASE_BUNDLE)
            finally:
                shutil.rmtree(spool, ignore_errors=True)
            sha = await _ok(["rev-parse", f"refs/heads/{default}"], bare)
            await _ok(["update-ref", base_ref, sha], bare)
            r = await self._box_git(
                box, "clone", "--branch", default, BASE_BUNDLE, REPO, cwd="/work"
            )
            if r.exit_code != 0:
                raise GitError(f"clone failed: {r.stderr.strip()}")
        elif not await self._ref_exists(bare, base_ref):
            r = await self._box_git(box, "rev-parse", f"origin/{default}")
            await _ok(["update-ref", base_ref, r.stdout.strip()], bare)
        for key, val in (("user.name", "factory-bot"), ("user.email", "factory-bot@localhost")):
            await self._box_git(box, "config", key, val)
        local = await self._box_git(box, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
        if local.exit_code == 0:
            r = await self._box_git(box, "switch", branch)
        else:
            remote = await self._box_git(
                box, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"
            )
            start = f"origin/{branch}" if remote.exit_code == 0 else f"origin/{default}"
            r = await self._box_git(box, "switch", "-c", branch, start)
        if r.exit_code != 0:
            raise GitError(f"switch failed: {r.stderr.strip()}")
        head = await self._box_git(box, "rev-parse", f"origin/{default}")
        return head.stdout.strip()

    async def rebase(self, job: Job, app: AppDef, box: Sandbox) -> bool:
        async with self._lock(app):
            if app.local_path is not None:
                app = await self._folder_sync(app)
            return await self._rebase(job, app, box)

    async def _rebase(self, job: Job, app: AppDef, box: Sandbox) -> bool:
        bare = self._bare(app)
        default = app.default_branch
        current = (await self._box_git(box, "rev-parse", f"origin/{default}")).stdout.strip()
        target = await _ok(["rev-parse", f"refs/heads/{default}"], bare)
        await _ok(["update-ref", f"refs/factory/base/{job.id}", target], bare)
        if current != target:
            spool = self._spool()
            try:
                bundle = None
                if await self._ref_exists(bare, f"{current}^{{commit}}"):
                    bundle = await self._make_base_bundle(
                        bare, app, spool, exclude=current, with_branch=None
                    )
                if bundle is None:
                    bundle = await self._make_base_bundle(
                        bare, app, spool, exclude=None, with_branch=None
                    )
                if bundle is None:
                    raise GitError(f"cannot bundle {default} of {app_slug(app)}")
                await box.exec(["mkdir", "-p", GIT_DIR], cwd="/work")
                await box.copy_in(bundle, BASE_BUNDLE)
            finally:
                shutil.rmtree(spool, ignore_errors=True)
            r = await self._box_git(
                box,
                "fetch",
                "--force",
                BASE_BUNDLE,
                f"+refs/heads/{default}:refs/remotes/origin/{default}",
            )
            if r.exit_code != 0:
                raise GitError(f"fetch failed: {r.stderr.strip()}")
        r = await self._box_git(box, "rebase", f"origin/{default}")
        if r.exit_code != 0:
            await self._box_git(box, "rebase", "--abort")
            return False
        return True

    async def diff_stat(self, box: Sandbox, base: str) -> DiffStat:
        return await sandbox_diff_stat(box, base)

    async def _pull_branch(self, job: Job, app: AppDef, box: Sandbox, branch: str) -> str:
        """Bundle the job branch in the sandbox and fetch it into the bare repo."""
        bare = self._bare(app)
        default = app.default_branch
        incoming = f"refs/factory/incoming/{job.id}"
        limit = self.max_bundle_mb * 2**20
        last = "no bundle"
        await box.exec(["mkdir", "-p", GIT_DIR], cwd="/work")
        for spec in (f"origin/{default}..{branch}", branch):
            await box.remove(JOB_BUNDLE)
            r = await self._box_git(box, "bundle", "create", JOB_BUNDLE, spec)
            if r.exit_code != 0:
                last = r.stderr.strip()
                continue
            spool = self._spool()
            try:
                local = spool / "job.bundle"
                await box.copy_out(JOB_BUNDLE, local, max_bytes=limit)
                rc, _, err = await _run(
                    ["fetch", "--force", str(local), f"+refs/heads/{branch}:{incoming}"], bare
                )
            finally:
                shutil.rmtree(spool, ignore_errors=True)
            if rc == 0:
                return await _ok(["rev-parse", incoming], bare)
            last = err.strip()
        raise GitError(f"cannot import branch {branch}: {last}")

    async def keep_branch(self, job: Job, app: AppDef, box: Sandbox) -> None:
        branch = branch_name(job.seq, job.slug)
        bare = self._bare(app)
        async with self._lock(app):
            head = await self._pull_branch(job, app, box, branch)
            await _ok(["update-ref", f"refs/heads/{branch}", head], bare)
            await _run(["update-ref", "-d", f"refs/factory/incoming/{job.id}"], bare)

    async def deliver(
        self, job: Job, app: AppDef, box: Sandbox, *, draft: bool, expected_head: str
    ) -> Delivery:
        if app.merge_policy != MergePolicy.LOCAL_MERGE:
            return Delivery(
                "failed",
                None,
                None,
                f"local repos only support local_merge, not {app.merge_policy}",
            )
        if app.local_path is not None and self.folders is None:
            return Delivery("failed", None, None, "FACTORY_HOST_REPOS_ROOT is not set")
        bare = self._bare(app)
        default = app.default_branch
        branch = branch_name(job.seq, job.slug)
        incoming_ref = f"refs/factory/incoming/{job.id}"
        async with self._lock(app):
            if app.local_path is not None:
                try:
                    app = await self._folder_sync(app)
                except FolderError as e:
                    return Delivery("failed", None, None, str(e))
                default = app.default_branch
            try:
                incoming = await self._pull_branch(job, app, box, branch)
            except TransferTooLarge as e:
                return Delivery("failed", None, None, str(e))
            except GitError as e:
                return Delivery("failed", None, None, str(e))
            try:
                if incoming != expected_head:
                    return Delivery("conflict", None, None, REASON_HEAD_MISMATCH)
                await _ok(["update-ref", f"refs/heads/{branch}", expected_head], bare)
                return await self._merge(job, app, bare, default, branch, expected_head)
            finally:
                await _run(["update-ref", "-d", incoming_ref], bare)

    async def _merge(
        self, job: Job, app: AppDef, bare: Path, default: str, branch: str, head: str
    ) -> Delivery:
        found = await _ok(
            [
                "log",
                f"refs/heads/{default}",
                "--merges",
                "--fixed-strings",
                "--grep",
                f"Factory-Job: {job.id}",
                "--format=%H",
            ],
            bare,
        )
        for sha in found.split():
            rc, out, _ = await _run(["rev-parse", f"{sha}^2"], bare)
            if rc == 0 and out.strip() == head:
                return Delivery("merged", None, sha, f"merged into {default}")
        rc, _, _ = await _run(["merge-base", "--is-ancestor", head, f"refs/heads/{default}"], bare)
        if rc == 0:
            return Delivery("failed", None, None, REASON_NOTHING_TO_DELIVER)
        old = await _ok(["rev-parse", f"refs/heads/{default}"], bare)
        folders = self.folders if app.local_path is not None else None
        if folders is not None:
            if old != await self.base_head(job, app):
                return Delivery("failed", None, None, REASON_FOLDER_MOVED)
            reason = await folders.check_clean(app)
            if reason:
                return Delivery("failed", None, None, reason)
        tmp = self._spool()
        wt = tmp / "wt"
        try:
            await _ok(["worktree", "add", "--detach", str(wt), old], bare)
            rc, out, err = await _run(
                ["merge", "--no-ff", "-m", f"Merge {branch}", "-m", f"Factory-Job: {job.id}", head],
                wt,
            )
            if rc != 0:
                await _run(["merge", "--abort"], wt)
                return Delivery("conflict", None, None, "merge conflict")
            new = await _ok(["rev-parse", "HEAD"], wt)
            rc, _, err = await _run(["update-ref", f"refs/heads/{default}", new, old], bare)
            if rc != 0:
                return Delivery("conflict", None, None, f"{default} moved during the merge")
            if folders is not None:
                reason = await folders.update_folder(app, branch=branch, merge_sha=new)
                if reason:
                    return Delivery("failed", None, None, reason)
            return Delivery("merged", None, new, f"merged into {default}")
        finally:
            await _run(["worktree", "remove", "--force", str(wt)], bare)
            shutil.rmtree(tmp, ignore_errors=True)
            await _run(["worktree", "prune"], bare)

    async def withdraw(self, job: Job, app: AppDef, *, note: str) -> None:
        return None
