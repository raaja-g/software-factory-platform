"""Credentialed git in a throwaway container on a platform-owned bare repo (never in a sandbox)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import docker
import requests
import structlog
from docker.errors import APIError, NotFound

from swfactory.db import repo as dbrepo
from swfactory.db.tables import Job
from swfactory.definition.models import MergePolicy
from swfactory.domain.types import Decision
from swfactory.github.auth import PERMS_FETCH, PERMS_PUSH, GitHubAuth
from swfactory.github.checks import CHECK_NAME, PrBodyContext, render_pr_body, review_check
from swfactory.github.client import GitHubClient, GitHubError, MutationQueue, PullRef
from swfactory.gitops.local import GitError, sandbox_diff_stat
from swfactory.gitops.service import (
    REASON_HEAD_MISMATCH,
    REASON_NOTHING_TO_DELIVER,
    SAFE_GIT_ENV,
    Delivery,
    DiffStat,
    GitService,
    safe_git,
)
from swfactory.logging import redact
from swfactory.queue import branch_name
from swfactory.sandbox.base import ExecResult, TransferTooLarge
from swfactory.sandbox.paths import REPO
from swfactory.sandbox.template import (
    GITOPS_ROOT,
    HARDENED_GIT_ENV,
    LABEL_JOB,
    LABEL_KIND,
    LABEL_MANAGED,
    gitops_config,
    gitops_volume,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from swfactory.artifacts import ArtifactStore
    from swfactory.definition.models import AppDef
    from swfactory.sandbox.base import Sandbox

log = structlog.get_logger(__name__)

__all__ = [
    "CREDENTIAL_HELPER",
    "GITOPS_REPO",
    "GITOPS_ROOT",
    "GITOPS_XFER",
    "HARDENED_GIT_ENV",
    "GitHubGit",
    "GitopsRunner",
    "credential_helper",
    "git_argv",
    "gitops_config",
    "gitops_volume",
    "hardened_git_args",
]

GITOPS_REPO = f"{GITOPS_ROOT}/repo.git"
GITOPS_XFER = f"{GITOPS_ROOT}/xfer"
_UID = 10001
_TAR_SLACK = 64 * 1024


def credential_helper(host: str = "github.com") -> str:
    """A git credential helper that answers `get` for this host only, from $GIT_TOKEN."""
    return (
        '!f() { test "$1" = get || exit 0; h=; '
        'while IFS= read -r l; do test -n "$l" || break; '
        f'test "$l" = "host={host}" && h=1; done; '
        'test -n "$h" || exit 0; echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f'
    )


CREDENTIAL_HELPER = credential_helper()


def hardened_git_args(*, host: str = "github.com") -> list[str]:
    cfg = [
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "credential.helper=",
        f"credential.https://{host}.helper={credential_helper(host)}",
        "protocol.allow=never",
        "protocol.https.allow=always",
    ]
    return [x for c in cfg for x in ("-c", c)]


def git_argv(
    op: Literal["init", "fetch", "push", "export_bundle", "import_bundle"],
    *,
    repo: str,
    branch: str | None = None,
    expect_sha: str | None = None,
    since: str | None = None,
    remote_base: str = "https://github.com",
) -> list[str]:
    host = remote_base.split("://", 1)[-1].split("/", 1)[0]
    base = ["git", *hardened_git_args(host=host)]
    url = f"{remote_base}/{repo}.git"
    if op == "init":
        return [*base, "init", "--bare", GITOPS_REPO]
    if branch is None:
        raise ValueError(f"{op} needs a branch")
    ref = f"refs/heads/{branch}"
    if op == "fetch":
        return [*base, "-C", GITOPS_REPO, "fetch", "--prune", url, f"+{ref}:{ref}"]
    if op == "push":
        return [
            *base,
            "-C",
            GITOPS_REPO,
            "push",
            f"--force-with-lease={ref}:{expect_sha or ''}",
            url,
            f"{ref}:{ref}",
        ]
    if op == "import_bundle":
        return [
            *base,
            "-c",
            "protocol.file.allow=always",
            "-C",
            GITOPS_REPO,
            "fetch",
            f"{GITOPS_XFER}/job.bundle",
            f"+{ref}:{ref}",
        ]
    excl = [f"^{since}"] if since else []
    return [*base, "-C", GITOPS_REPO, "bundle", "create", f"{GITOPS_XFER}/base.bundle", *excl, ref]


def _op_name(argv: Sequence[str]) -> str:
    if "-C" in argv:
        i = argv.index("-C") + 2
        if i < len(argv):
            return str(argv[i])
    return str(argv[0]) if argv else "command"


def _bootstrap_tar() -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in ("repo.git", "xfer"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = info.gid = _UID
            info.mtime = int(time.time())
            tf.addfile(info)
    buf.seek(0)
    return buf


class GitopsRunner:
    def __init__(
        self,
        client_factory: Callable[[], docker.DockerClient],
        *,
        image: str,
        network: str = "gitops-net",
        default_timeout_s: int = 900,
        spool_dir: Path | None = None,
        output_max_bytes: int = 2_000_000,
    ) -> None:
        self._factory = client_factory
        self._client_: docker.DockerClient | None = None
        self._image = image
        self._network = network
        self._timeout = default_timeout_s
        self._out_max = output_max_bytes
        self._spool_dir = spool_dir or (
            Path(os.environ.get("FACTORY_REPOS_DIR", "/repos")) / ".spool"
        )

    @property
    def _client(self) -> docker.DockerClient:
        if self._client_ is None:
            self._client_ = self._factory()
        return self._client_

    # -- volume and containers (sync, run in threads) -----------------------
    def _ensure_volume(self, job_id: UUID) -> str:
        name = gitops_volume(job_id)
        try:
            self._client.volumes.get(name)
            return name
        except NotFound:
            pass
        self._client.volumes.create(
            name=name,
            labels={LABEL_JOB: str(job_id), LABEL_MANAGED: "true", LABEL_KIND: "gitops"},
        )
        cname = f"gitops-boot-{job_id}"
        cfg = gitops_config(
            job_id, image=self._image, network=self._network, argv=["true"], token=None
        )
        self._create(cname, cfg)
        try:
            ok = self._client.api.put_archive(cname, GITOPS_ROOT, _bootstrap_tar())
            if not ok:
                raise RuntimeError("cannot prepare the gitops volume")
        finally:
            self._rm(cname)
        return name

    def _create(self, name: str, cfg: dict[str, Any]) -> None:
        self._client.api.create_container(
            image=cfg["Image"],
            command=cfg["Cmd"],
            name=name,
            user=cfg["User"],
            working_dir=cfg["WorkingDir"],
            environment=cfg["Env"],
            labels=cfg["Labels"],
            host_config=cfg["HostConfig"],
            networking_config=cfg["NetworkingConfig"],
        )

    def _rm(self, name: str) -> None:
        with contextlib.suppress(NotFound, APIError):
            self._client.api.remove_container(name, force=True, v=False)

    def _run_sync(
        self, job_id: UUID, argv: Sequence[str], token: str | None, timeout_s: int
    ) -> ExecResult:
        self._ensure_volume(job_id)
        name = f"gitops-run-{job_id}-{os.urandom(4).hex()}"
        cfg = gitops_config(
            job_id, image=self._image, network=self._network, argv=argv, token=token
        )
        start = time.monotonic()
        self._create(name, cfg)
        api = self._client.api
        try:
            api.start(name)
            try:
                res = api.wait(name, timeout=timeout_s)
                code = int(res.get("StatusCode", -1))
                timed_out = False
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
                with contextlib.suppress(APIError, NotFound):
                    api.stop(name, timeout=2)
                code, timed_out = 124, True
            out = api.logs(name, stdout=True, stderr=False)[-self._out_max :]
            err = api.logs(name, stdout=False, stderr=True)[-self._out_max :]
        finally:
            self._rm(name)
        stdout = self._clean(out, token)
        stderr = self._clean(err, token)
        if timed_out:
            stderr += (
                f"\ngit {_op_name(argv)} timed out after {timeout_s} s; "
                "raise FACTORY_GITOPS_TIMEOUT_S"
            )
        return ExecResult(code, stdout, stderr, timed_out, time.monotonic() - start)

    @staticmethod
    def _clean(raw: bytes, token: str | None) -> str:
        text = raw.decode("utf-8", errors="replace")
        if token:
            text = text.replace(token, "[REDACTED]")
        return redact(text)

    async def run(
        self,
        job_id: UUID,
        argv: Sequence[str],
        *,
        token: str | None,
        timeout_s: int | None = None,
    ) -> ExecResult:
        t = self._timeout if timeout_s is None else timeout_s
        return await asyncio.to_thread(self._run_sync, job_id, argv, token, t)

    # -- transfers ----------------------------------------------------------
    def _spool(self) -> Path:
        self._spool_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self._spool_dir, prefix="gitops-xfer-")
        os.close(fd)
        return Path(name)

    def _holder(self, job_id: UUID) -> str:
        self._ensure_volume(job_id)
        name = f"gitops-xfer-{job_id}-{os.urandom(4).hex()}"
        cfg = gitops_config(
            job_id, image=self._image, network=self._network, argv=["true"], token=None
        )
        self._create(name, cfg)  # created, never started
        return name

    def _put_sync(self, job_id: UUID, path: str, src: Path) -> None:
        directory, base = path.rsplit("/", 1)
        spool = self._spool()
        holder = self._holder(job_id)
        try:
            with spool.open("wb") as f, tarfile.open(fileobj=f, mode="w") as tf:
                info = tf.gettarinfo(str(src), arcname=base)
                info.uid = info.gid = _UID
                info.mode = 0o644
                with src.open("rb") as sf:
                    tf.addfile(info, sf)
            with spool.open("rb") as f:
                if not self._client.api.put_archive(holder, directory, f):
                    raise RuntimeError(f"cannot copy into {path}")
        finally:
            spool.unlink(missing_ok=True)
            self._rm(holder)

    async def put_file(self, job_id: UUID, path: str, src: Path) -> None:
        await asyncio.to_thread(self._put_sync, job_id, path, src)

    def _get_sync(self, job_id: UUID, path: str, dest: Path, max_bytes: int) -> int:
        holder = self._holder(job_id)
        spool = self._spool()
        try:
            try:
                stream, stat = self._client.api.get_archive(holder, path)
            except NotFound as e:
                raise FileNotFoundError(path) from e
            if int(stat.get("size", 0)) > max_bytes:
                raise TransferTooLarge(max_bytes)
            received = 0
            with spool.open("wb") as f:
                for chunk in stream:
                    received += len(chunk)
                    if received > max_bytes + _TAR_SLACK:
                        raise TransferTooLarge(max_bytes)
                    f.write(chunk)
            with spool.open("rb") as f, tarfile.open(fileobj=f, mode="r|") as tf:
                for member in tf:
                    if not member.isfile():
                        continue
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    if member.size > max_bytes:
                        raise TransferTooLarge(max_bytes)
                    try:
                        with dest.open("wb") as out:
                            shutil.copyfileobj(src, out, 1 << 20)
                    except BaseException:
                        dest.unlink(missing_ok=True)
                        raise
                    return member.size
            raise FileNotFoundError(path)
        finally:
            spool.unlink(missing_ok=True)
            self._rm(holder)

    async def get_file(self, job_id: UUID, path: str, dest: Path, *, max_bytes: int) -> int:
        return await asyncio.to_thread(self._get_sync, job_id, path, dest, max_bytes)

    def _remove_volume_sync(self, job_id: UUID) -> None:
        with contextlib.suppress(NotFound):
            self._client.volumes.get(gitops_volume(job_id)).remove(force=True)

    async def remove_volume(self, job_id: UUID) -> None:
        await asyncio.to_thread(self._remove_volume_sync, job_id)


# -- GitHubGit (M8.4b) -------------------------------------------------------------------

GIT_DIR = "/work/.factory/git"
BASE_BUNDLE = f"{GIT_DIR}/base.bundle"
KEPT_BUNDLE = f"{GIT_DIR}/kept.bundle"
JOB_BUNDLE = f"{GIT_DIR}/job.bundle"
LABEL_NEEDS_HUMAN = "factory:needs-human"
FALLBACK_IDENTITY = ("factory-bot", "factory-bot@users.noreply.github.com")
REASON_BRANCH_CHANGED = "branch changed on GitHub"
REASON_PR_CLOSED = "PR closed on GitHub"
REASON_FOLLOW_UP_PR_ENDED = "the PR was merged or closed during the follow-up"
MERGE_POLL_S = 2.0
MERGE_TIMEOUT_S = 120.0
_PLATFORM_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")}


def repo_of(app: AppDef) -> str:
    """``owner/name`` of a GitHub app's ``repo_url``."""
    url = app.repo_url.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        url = url.removeprefix(prefix)
    return url.removesuffix(".git")


def _first_paragraph(text: str) -> str:
    for block in text.split("\n\n"):
        block = block.strip()
        if block and not block.startswith("#"):
            return block[:800]
    return ""


def _criteria(text: str) -> tuple[str, ...]:
    out: list[str] = []
    on = False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            on = "criteria" in line.lower()
            continue
        if on and line.lstrip().startswith(("-", "*")):
            item = line.lstrip().lstrip("-* ").strip()
            item = item.removeprefix("[ ]").removeprefix("[x]").strip()
            if item:
                out.append(item)
    return tuple(out)


_FETCH_PR_HEAD = "fetch-pr-head"


class GitHubGit(GitService):
    """Delivery to a GitHub repo: credentialed git in the gitops container, PR via the client."""

    def __init__(
        self,
        *,
        auth: GitHubAuth,
        gitops: GitopsRunner,
        sessions: async_sessionmaker[AsyncSession],
        repos_dir: Path,
        client_for: Callable[[str], GitHubClient],
        artifacts: ArtifactStore | None = None,
        max_bundle_mb: int = 2048,
        dashboard_url: str = "",
        remote_base: str = "https://github.com",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.auth = auth
        self.gitops = gitops
        self.sessions = sessions
        self.repos_dir = Path(repos_dir)
        self.client_for = client_for
        self.artifacts = artifacts
        self.max_bundle_mb = max_bundle_mb
        self.dashboard_url = dashboard_url.rstrip("/")
        self.remote_base = remote_base
        self._sleep = sleep
        self._identity: tuple[str, str] | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    # -- plumbing ---------------------------------------------------------------------

    @property
    def _host(self) -> str:
        return self.remote_base.split("://", 1)[-1].split("/", 1)[0]

    @property
    def _limit(self) -> int:
        return self.max_bundle_mb * 2**20

    def _spool(self) -> Path:
        d = self.repos_dir / ".spool"
        d.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=d))

    async def _git_op(
        self, job: Job, op: Any, app: AppDef, *, branch: str, token: str | None = None, **kw: Any
    ) -> ExecResult:
        argv = git_argv(op, repo=repo_of(app), branch=branch, remote_base=self.remote_base, **kw)
        return await self.gitops.run(job.id, argv, token=token)

    async def _gitops_git(self, job: Job, *args: str) -> ExecResult:
        argv = ["git", *hardened_git_args(host=self._host), "-C", GITOPS_REPO, *args]
        return await self.gitops.run(job.id, argv, token=None)

    async def _rev(self, job: Job, ref: str) -> str | None:
        r = await self._gitops_git(job, "rev-parse", "--verify", "-q", ref)
        return r.stdout.strip() if r.exit_code == 0 and r.stdout.strip() else None

    async def _fetch_default(self, job: Job, app: AppDef) -> str:
        """Fetch the default branch into the job's gitops repo; its sha."""
        default = app.default_branch
        r = await self._git_op(job, "init", app, branch=default)
        if r.exit_code != 0:
            raise GitError(f"git init failed: {r.stderr.strip()}")
        token = await self.auth.token_for(repo_of(app), PERMS_FETCH)
        r = await self._git_op(job, "fetch", app, branch=default, token=token)
        if r.exit_code != 0:
            raise GitError(f"cannot fetch {default}: {r.stderr.strip()}")
        sha = await self._rev(job, f"refs/heads/{default}")
        if sha is None:
            raise GitError(f"cannot read {default} after the fetch")
        return sha

    async def _export(
        self, job: Job, app: AppDef, box: Sandbox, ref_branch: str, dest: str, *, since: str | None
    ) -> None:
        """Bundle ``ref_branch`` in the gitops repo and put it in the sandbox at ``dest``."""
        r = await self._git_op(job, "export_bundle", app, branch=ref_branch, since=since)
        if r.exit_code != 0:
            raise GitError(f"cannot bundle {ref_branch}: {r.stderr.strip()}")
        spool = self._spool()
        try:
            local = spool / "out.bundle"
            await self.gitops.get_file(
                job.id, f"{GITOPS_XFER}/base.bundle", local, max_bytes=self._limit
            )
            await box.exec(["mkdir", "-p", GIT_DIR], cwd="/work")
            await box.copy_in(local, dest)
        finally:
            shutil.rmtree(spool, ignore_errors=True)

    def _platform_bare(self, app: AppDef) -> Path:
        owner, _, name = repo_of(app).partition("/")
        return self.repos_dir / "github" / owner / f"{name}.git"

    async def _platform_git(self, *args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        argv = ["git", *hardened_git_args(host=self._host), "-c", "protocol.file.allow=always"]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            *args,
            cwd=cwd,
            env={**_PLATFORM_ENV, **HARDENED_GIT_ENV},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, out.decode().strip(), err.decode().strip()

    async def _bot_identity(self, app: AppDef) -> tuple[str, str]:
        if self._identity is None:
            try:
                self._identity = await self.client_for(repo_of(app)).bot_identity()
            except GitHubError:
                return FALLBACK_IDENTITY
        return self._identity

    # -- GitService: prepare, rebase, base_head ------------------------------------------

    def _lock(self, job: Job) -> asyncio.Lock:
        return self._locks.setdefault(str(job.id), asyncio.Lock())

    async def prepare(self, job: Job, app: AppDef, box: Sandbox) -> str:
        async with self._lock(job):
            return await self._prepare(job, app, box)

    async def _prepare(self, job: Job, app: AppDef, box: Sandbox) -> str:
        default = app.default_branch
        branch = _branch(job)
        has_repo = (await box.read_bytes(f"{REPO}/.git/HEAD")) is not None
        if not has_repo:
            await self._fetch_default(job, app)
            async with self.sessions() as s:
                pushed, _ = await dbrepo.delivery_state(s, job.id)
            follow_up = job.follow_up_of is not None
            kept = await self._restore_kept(
                job, app, box, branch, pushed=pushed or (_FETCH_PR_HEAD if follow_up else None)
            )
            if follow_up and not pushed:
                head = await self._rev(job, f"refs/heads/{branch}")
                if head is None:
                    raise GitError(f"cannot read {branch} after the fetch")
                await self._record_pushed(job, head)
            await self._export(job, app, box, default, BASE_BUNDLE, since=None)
            r = await _box(box, "clone", "--branch", default, BASE_BUNDLE, REPO, cwd="/work")
            if r.exit_code != 0:
                raise GitError(f"clone failed: {r.stderr.strip()}")
            if kept:
                r = await _box(
                    box,
                    "fetch",
                    "--force",
                    KEPT_BUNDLE,
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                )
                if r.exit_code != 0:
                    raise GitError(f"cannot restore {branch}: {r.stderr.strip()}")
        name, email = await self._bot_identity(app)
        await _box(box, "config", "user.name", name)
        await _box(box, "config", "user.email", email)
        local = await _box(box, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
        if local.exit_code == 0:
            r = await _box(box, "switch", branch)
        else:
            remote = await _box(box, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}")
            start = f"origin/{branch}" if remote.exit_code == 0 else f"origin/{default}"
            r = await _box(box, "switch", "-c", branch, start)
        if r.exit_code != 0:
            raise GitError(f"switch failed: {r.stderr.strip()}")
        head = await _box(box, "rev-parse", f"origin/{default}")
        return head.stdout.strip()

    async def _restore_kept(
        self, job: Job, app: AppDef, box: Sandbox, branch: str, *, pushed: str | None
    ) -> bool:
        """The kept branch's bundle into the sandbox: from GitHub if pushed, else the bare repo."""
        if pushed:
            token = await self.auth.token_for(repo_of(app), PERMS_FETCH)
            r = await self._git_op(job, "fetch", app, branch=branch, token=token)
            if r.exit_code != 0:
                raise GitError(f"cannot fetch {branch}: {r.stderr.strip()}")
            await self._export(job, app, box, branch, KEPT_BUNDLE, since=None)
            return True
        bare = self._platform_bare(app)
        if not bare.exists():
            return False
        rc, _, _ = await self._platform_git(
            "rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=bare
        )
        if rc != 0:
            return False
        spool = self._spool()
        try:
            bundle = spool / "kept.bundle"
            rc, _, err = await self._platform_git(
                "bundle", "create", str(bundle), f"refs/heads/{branch}", cwd=bare
            )
            if rc != 0:
                raise GitError(f"cannot bundle kept {branch}: {err}")
            await box.exec(["mkdir", "-p", GIT_DIR], cwd="/work")
            await box.copy_in(bundle, KEPT_BUNDLE)
        finally:
            shutil.rmtree(spool, ignore_errors=True)
        return True

    async def rebase(self, job: Job, app: AppDef, box: Sandbox) -> bool:
        async with self._lock(job):
            default = app.default_branch
            target = await self._fetch_default(job, app)
            current = (await _box(box, "rev-parse", f"origin/{default}")).stdout.strip()
            if current != target:
                since = None
                if current and await self._rev(job, f"{current}^{{commit}}") is not None:
                    since = current
                await self._export(job, app, box, default, BASE_BUNDLE, since=since)
                r = await _box(
                    box,
                    "fetch",
                    "--force",
                    BASE_BUNDLE,
                    f"+refs/heads/{default}:refs/remotes/origin/{default}",
                )
                if r.exit_code != 0:
                    raise GitError(f"fetch failed: {r.stderr.strip()}")
            r = await _box(box, "rebase", f"origin/{default}")
            if r.exit_code != 0:
                await _box(box, "rebase", "--abort")
                return False
            return True

    async def base_head(self, job: Job, app: AppDef) -> str:
        sha = await self._rev(job, f"refs/heads/{app.default_branch}")
        if sha is None:
            raise GitError(f"no {app.default_branch} in the gitops repo of job {job.id}")
        return sha

    async def diff_stat(self, box: Sandbox, base: str) -> DiffStat:
        return await sandbox_diff_stat(box, base)

    # -- step 2 of deliver: move the job's commits, push with the persisted lease -----------

    async def _bundle_out(
        self, app: AppDef, box: Sandbox, branch: str, dest: Path, *, whole: bool
    ) -> None:
        """Bundle the job's commits (or, with ``whole``, the whole branch) out of the sandbox."""
        await box.exec(["mkdir", "-p", GIT_DIR], cwd="/work")
        specs = [branch] if whole else [f"origin/{app.default_branch}..{branch}", branch]
        r = None
        for spec in specs:
            await box.remove(JOB_BUNDLE)
            r = await _box(box, "bundle", "create", JOB_BUNDLE, spec)
            if r.exit_code == 0:
                break
        else:
            raise GitError(f"cannot bundle {branch}: {r.stderr.strip() if r else ''}")
        await box.copy_out(JOB_BUNDLE, dest, max_bytes=self._limit)

    async def _transfer(self, job: Job, app: AppDef, box: Sandbox, branch: str) -> str:
        """Bundle the branch, import it into the gitops repo; the branch's sha there."""
        spool = self._spool()
        try:
            local = spool / "job.bundle"
            await self._bundle_out(app, box, branch, local, whole=False)
            await self.gitops.put_file(job.id, f"{GITOPS_XFER}/job.bundle", local)
        finally:
            shutil.rmtree(spool, ignore_errors=True)
        r = await self._git_op(job, "import_bundle", app, branch=branch)
        if r.exit_code != 0:  # a missing prerequisite or repo: the gitops volume was lost
            await self._fetch_default(job, app)
            r = await self._git_op(job, "import_bundle", app, branch=branch)
        if r.exit_code != 0:
            raise GitError(f"cannot import {branch}: {r.stderr.strip()}")
        head = await self._rev(job, f"refs/heads/{branch}")
        if head is None:
            raise GitError(f"cannot read {branch} after the import")
        return head

    async def _is_ancestor_of_default(self, job: Job, app: AppDef, head: str) -> bool:
        r = await self._gitops_git(
            job, "merge-base", "--is-ancestor", head, f"refs/heads/{app.default_branch}"
        )
        return r.exit_code == 0

    async def _push(
        self, job: Job, app: AppDef, gh: GitHubClient, branch: str, head: str
    ) -> str | None:
        """Push ``head`` unless GitHub has it; None on success, else a conflict reason."""
        if await gh.branch_sha(branch) == head:
            await self._record_pushed(job, head)
            return None
        async with self.sessions() as s:
            lease, _ = await dbrepo.delivery_state(s, job.id)
        token = await self.auth.token_for(repo_of(app), PERMS_PUSH)
        r = await self._git_op(job, "push", app, branch=branch, token=token, expect_sha=lease or "")
        if r.exit_code != 0:
            err = (r.stderr + r.stdout).lower()
            if "stale info" in err or "rejected" in err or "fetch first" in err:
                return REASON_BRANCH_CHANGED
            raise GitError(f"push failed: {r.stderr.strip()}")
        await self._record_pushed(job, head)
        return None

    async def _record_pushed(self, job: Job, sha: str) -> None:
        async with self.sessions.begin() as s:
            await dbrepo.record_pushed(s, job.id, sha)

    # -- keep_branch ---------------------------------------------------------------------

    async def keep_branch(self, job: Job, app: AppDef, box: Sandbox) -> None:
        branch = _branch(job)
        async with self._lock(job):
            if not app.github.ci_safe:
                await self._keep_on_platform(job, app, box, branch)
                return
            head = await self._transfer(job, app, box, branch)
            if app.github.ci_safe:
                if await self._is_ancestor_of_default(job, app, head):
                    return
                reason = await self._push(job, app, self.client_for(repo_of(app)), branch, head)
                if reason is not None:
                    log.warning("keep_branch_not_pushed", job=str(job.id), reason=reason)
                return

    async def _keep_on_platform(self, job: Job, app: AppDef, box: Sandbox, branch: str) -> None:
        """No token, nothing to GitHub: save the whole branch in the platform bare repo."""
        bare = self._platform_bare(app)
        if not bare.exists():
            bare.parent.mkdir(parents=True, exist_ok=True)
            rc, _, err = await self._platform_git("init", "--bare", str(bare))
            if rc != 0:
                raise GitError(f"cannot create {bare}: {err}")
        spool = self._spool()
        try:
            local = spool / "kept.bundle"
            await self._bundle_out(app, box, branch, local, whole=True)
            rc, _, err = await self._platform_git(
                "fetch",
                "--force",
                str(local),
                f"+refs/heads/{branch}:refs/heads/{branch}",
                cwd=bare,
            )
            if rc != 0:
                raise GitError(f"cannot keep {branch}: {err}")
        finally:
            shutil.rmtree(spool, ignore_errors=True)

    # -- deliver ----------------------------------------------------------------------------

    async def withdraw(self, job: Job, app: AppDef, *, note: str) -> None:
        """Comment once on the job's open PR, then close it; a closed or missing PR is left."""
        gh = self.client_for(repo_of(app))
        pr = await gh.find_pr(_branch(job))
        if pr is None or pr.state != "open":
            return
        marker = f"factory:withdraw:{job.id}"
        await gh.comment_once(pr.number, f"{note}\n\n<!-- {marker} -->", marker=marker)
        await gh.close_pr(pr.number)

    async def deliver(
        self, job: Job, app: AppDef, box: Sandbox, *, draft: bool, expected_head: str
    ) -> Delivery:
        async with self._lock(job):
            try:
                return await self._deliver(job, app, box, draft=draft, expected_head=expected_head)
            except TransferTooLarge as e:
                return Delivery("failed", None, None, str(e))
            except GitError as e:
                return Delivery("failed", None, None, str(e))

    async def _deliver(
        self, job: Job, app: AppDef, box: Sandbox, *, draft: bool, expected_head: str
    ) -> Delivery:
        gh = self.client_for(repo_of(app))
        branch = _branch(job)
        pr = await gh.find_pr(branch)
        if (
            job.follow_up_of is not None
            and pr is not None
            and (pr.merged or pr.state == "closed")
            and pr.head_sha != expected_head
        ):
            return Delivery("failed", pr.url, None, REASON_FOLLOW_UP_PR_ENDED)
        if pr is not None and pr.merged:
            return Delivery("merged", pr.url, pr.merge_sha, "merged on GitHub")
        if pr is not None and pr.state == "closed":
            return Delivery("failed", pr.url, None, REASON_PR_CLOSED)
        head = await self._transfer(job, app, box, branch)
        if head != expected_head:
            return Delivery("conflict", pr.url if pr else None, None, REASON_HEAD_MISMATCH)
        if await self._is_ancestor_of_default(job, app, head):
            return Delivery("failed", None, None, REASON_NOTHING_TO_DELIVER)
        reason = await self._push(job, app, gh, branch, head)
        if reason is not None:
            return Delivery("conflict", pr.url if pr else None, None, reason)
        ctx = await self._context(job, app, head)
        body = render_pr_body(ctx)
        if pr is None:
            pr = await gh.create_pr(
                head=branch, base=app.default_branch, title=_title(job, ctx), body=body
            )
        else:
            await gh.update_pr_body(pr.number, body)
        await gh.add_labels(pr.number, [app.github.intake_label])
        conclusion, title, summary = review_check(ctx)
        await gh.upsert_check_run(
            head_sha=head,
            name=CHECK_NAME,
            conclusion=conclusion,
            title=title,
            summary=summary,
            details_url=ctx.dashboard_url,
        )
        escalated = ctx.decision == Decision.ESCALATE.value and ctx.approved_by is None
        if escalated:
            await gh.add_labels(pr.number, [LABEL_NEEDS_HUMAN])
            rnd = await self._decision_round(job)
            await gh.comment_once(
                pr.number,
                f"The agents could not approve this change on their own: "
                f"{ctx.decision_reason or 'a person needs to review it'}.\n\n"
                f"[Open in the dashboard]({ctx.dashboard_url})\n\n"
                f"<!-- factory:escalate:{job.id}:{rnd} -->",
                marker=f"factory:escalate:{job.id}:{rnd}",
            )
        if draft:
            return Delivery("pr_open", pr.url, None, "draft PR open")
        if pr.draft:
            await gh.mark_ready(pr)
        await gh.remove_label(pr.number, LABEL_NEEDS_HUMAN)
        if app.merge_policy == MergePolicy.PR_AUTO_MERGE:
            return await self._auto_merge(job, app, gh, pr, head)
        if app.merge_policy == MergePolicy.PR_MANUAL:
            return Delivery("pr_open", pr.url, None, "PR ready for a person to merge")
        return Delivery("failed", pr.url, None, f"github apps cannot use {app.merge_policy}")

    async def _decision_round(self, job: Job) -> int:
        async with self.sessions() as s:
            found = await dbrepo.latest_decision_event(s, job.id)
        return int((found[0] if found else {}).get("round", 0))

    async def _auto_merge(
        self, job: Job, app: AppDef, gh: GitHubClient, pr: PullRef, head: str
    ) -> Delivery:
        method = app.github.merge_method
        async with self.sessions() as s:
            _, uuid = await dbrepo.delivery_state(s, job.id)
        if not uuid:
            try:
                uuid = await gh.merge_async(pr.number, sha=head, method=method)
            except GitHubError as e:
                if e.status == 409:
                    return Delivery("conflict", pr.url, None, str(e))
                if e.status == 405 and "blocked" in str(e).lower():
                    await gh.enable_auto_merge(pr, expected_head_oid=head, method=method)
                    return Delivery("pr_open", pr.url, None, "auto-merge enabled on GitHub")
                return Delivery("failed", pr.url, None, str(e))
            async with self.sessions.begin() as s:
                await dbrepo.record_merge_request(s, job.id, uuid)
        waited = 0.0
        while True:
            status = await gh.merge_async_status(pr.number, uuid)
            if status == "merged":
                done = await gh.get_pr(pr.number)
                return Delivery("merged", done.url, done.merge_sha, "merged on GitHub")
            if status == "failed":
                async with self.sessions.begin() as s:
                    await dbrepo.record_merge_request(s, job.id, None)
                return Delivery("failed", pr.url, None, "GitHub could not merge the PR")
            if waited >= MERGE_TIMEOUT_S:
                return Delivery("pr_open", pr.url, None, "merge queued on GitHub")
            await self._sleep(MERGE_POLL_S)
            waited += MERGE_POLL_S

    async def _context(self, job: Job, app: AppDef, head: str) -> PrBodyContext:
        async with self.sessions() as s:
            row = await s.get(Job, job.id)
            feature = row.feature if row else job.feature
            decision = await dbrepo.latest_decision_event(s, job.id)
            approved_by = await dbrepo.approver_after(s, job.id, decision[1] if decision else None)
            verdicts, runs = await dbrepo.review_rows(s, job.id)
        spec = verify = ""
        if self.artifacts is not None:
            spec = await self.artifacts.get_text(job.id, "spec.md") or ""
            verify = await self.artifacts.get_text(job.id, "verify.md") or ""
        data = decision[0] if decision else {}
        return PrBodyContext(
            job_id=job.id,
            feature=feature,
            spec_summary=_first_paragraph(spec),
            criteria=_criteria(spec),
            verify=verify[:20_000],
            screenshots=(),
            verdicts=verdicts,
            decision=str(data["decision"]) if "decision" in data else None,
            decision_reason=str(data.get("reason", "")),
            runs=tuple(runs),
            dashboard_url=f"{self.dashboard_url}/#/job/{job.id}",
            approved_by=approved_by,
        )


def _branch(job: Job) -> str:
    """A follow-up works on the PR branch of the job it continues, any other job on its own."""
    if getattr(job, "follow_up_of", None) is not None and job.branch:
        return job.branch
    return branch_name(job.seq, job.slug)


def _title(job: Job, ctx: PrBodyContext) -> str:
    first = ctx.feature.strip().splitlines()[0] if ctx.feature.strip() else ""
    return (job.title or first or "Factory change")[:200]


async def _box(box: Sandbox, *args: str, cwd: str = REPO) -> ExecResult:
    return await box.exec(safe_git(*args), cwd=cwd, env=SAFE_GIT_ENV)


def build_github_git(
    settings: Any,
    sessions: async_sessionmaker[AsyncSession],
    artifacts: ArtifactStore | None,
    docker_client: Callable[[], docker.DockerClient],
) -> GitHubGit:
    """The production GitHubGit: App or PAT auth, one serial mutation queue per process."""
    from swfactory.github.auth import make_auth
    from swfactory.github.client import MutationQueue

    auth = make_auth(settings)
    queue = MutationQueue()
    runner = GitopsRunner(
        docker_client,
        image=settings.gitops_image,
        network=settings.gitops_network,
        default_timeout_s=settings.gitops_timeout_s,
    )
    return GitHubGit(
        auth=auth,
        gitops=runner,
        sessions=sessions,
        repos_dir=Path(settings.repos_dir),
        client_for=lambda repo: GitHubClient(auth, repo, queue),
        artifacts=artifacts,
        max_bundle_mb=settings.max_bundle_mb,
        dashboard_url=(settings.public_url or settings.api_url or ""),
    )


PERMS_PROVISION = {"administration": "write", "contents": "write", "metadata": "read"}


@dataclass(frozen=True, slots=True)
class ProvisionedRepo:
    full_name: str
    url: str
    default_branch: str
    reachable: bool


async def provision_repo(
    auth: GitHubAuth, *, full_name: str, template: str | None, private: bool
) -> ProvisionedRepo:
    """Create ``full_name`` (empty, or from a template) with auto-merge on, unless it exists.

    The factory never adds the repo to the App installation (that needs a user token), so
    ``reachable`` tells the caller whether a person still has to.
    """
    client = GitHubClient(auth, full_name, MutationQueue())
    try:
        info = await client.repo_info(PERMS_PROVISION)
        if info is None:
            if template:
                info = await client.generate_from_template(
                    template, private=private, permissions=PERMS_PROVISION
                )
            else:
                # Only a personal-mode PAT can own a repo under a user; Apps use /orgs.
                user_owned = not hasattr(auth, "app_headers") and await client.owner_is_user()
                info = await client.create_org_repo(
                    private=private, permissions=PERMS_PROVISION, user_owned=user_owned
                )
        reachable = await client.installation_reachable()
    finally:
        await client.aclose()
    return ProvisionedRepo(
        full_name=str(info.get("full_name") or full_name),
        url=str(info.get("html_url") or f"https://github.com/{full_name}"),
        default_branch=str(info.get("default_branch") or "main"),
        reachable=reachable,
    )
