"""Docker sandbox provider: per-job networks, hardened containers, isolated clones."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
import structlog
from docker.errors import APIError, NotFound

from swfactory.gitops.service import SAFE_GIT_ENV
from swfactory.sandbox import paths
from swfactory.sandbox.base import (
    READ_BYTES_MAX,
    ExecResult,
    RunnerSpec,
    SandboxFileTooLarge,
    TransferTooLarge,
    WorkspaceOverLimit,
)
from swfactory.sandbox.disk import HeadTailBuffer, over_work_limit
from swfactory.sandbox.reaper import Labelled, select_orphans
from swfactory.sandbox.template import (
    EPHEMERAL_TTL,
    LABEL_EXPIRES,
    LABEL_JOB,
    LABEL_MANAGED,
    LABEL_MAX_WORK_GB,
    SANDBOX_TTL,
    container_config,
    container_name,
    home_volume_name,
    isolated_names,
    job_network_name,
    network_create_args,
    verified_volume_name,
    volume_name,
)

log = structlog.get_logger(__name__)

EDGE_ALIAS = "gateway-edge"
ISOLATED_GIT_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
LABEL_HEAD = "factory.head"
COMPOSE_SERVICE = "com.docker.compose.service"
_STAGE_DIR = "/work/.factory/tmp"
_UID = 10001
_TAR_SLACK = 64 * 1024
_SETUP_FLAGS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.file.allow=always",
)
_MISSING = 3

_ISOLATED_SETUP = (
    "set -e; cp -a /snapshot/repo /work/repo; mkdir -p /work/.factory/out; "
    "cp -a /snapshot/.factory/in /work/.factory/in; cd /work/repo; "
    "rm -rf .git/hooks .git/info; "
    "git for-each-ref --format='%(refname)' refs/replace/ | xargs -r -n1 git update-ref -d; "
    'printf %s "$1" > .git/config; '
    "git -c core.hooksPath=/dev/null reset -q --hard HEAD && git clean -fdq"
)


def _expires(ttl: Any) -> datetime:
    return datetime.now(UTC) + ttl


class _IterReader(io.RawIOBase):
    """A readable file object over an iterator of byte chunks."""

    def __init__(self, chunks: Any) -> None:
        self._it = iter(chunks)
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        while not self._buf:
            try:
                self._buf = next(self._it)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def _tar_one(name: str, data: bytes) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o644
        info.uid = info.gid = _UID
        info.mtime = int(time.time())
        tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


class DockerSandbox:
    def __init__(
        self,
        client: docker.DockerClient,
        job_id: UUID,
        name: str,
        image_digest: str,
        *,
        home_volume: bool,
        spool_dir: Path,
        output_max_bytes: int = 2_000_000,
        marks: dict[UUID, int] | None = None,
    ) -> None:
        self._client = client
        self._output_max = output_max_bytes
        self._marks = marks if marks is not None else {}
        self.job_id = job_id
        self.name = name
        self.image_digest = image_digest
        self._archive_roots = ("/work", paths.HOME) if home_volume else ("/work",)
        self._spool_dir = spool_dir
        self._count = 0

    # -- helpers ---------------------------------------------------------
    def _check(self) -> None:
        limit = self._marks.get(self.job_id)
        if limit is not None:
            raise WorkspaceOverLimit(f"workspace over {limit} GB")

    def _archive_ok(self, path: str) -> bool:
        return any(path == r or path.startswith(r + "/") for r in self._archive_roots)

    def _exec_sync(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None,
    ) -> tuple[int, str, str]:
        result = self._exec_once(argv, cwd=cwd, env=env)
        if cwd == paths.REPO and result[0] in (126, 127) and "chdir to cwd" in result[1]:
            # the default cwd (the repo) does not exist yet: fall back to /work
            return self._exec_once(argv, cwd="/work", env=env)
        return result

    def _exec_once(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None,
    ) -> tuple[int, str, str]:
        api = self._client.api
        ex = api.exec_create(
            self.name,
            list(argv),
            user=f"{_UID}:{_UID}",
            workdir=cwd,
            environment=dict(env) if env else None,
            stdout=True,
            stderr=True,
        )
        out_buf = HeadTailBuffer(self._output_max)
        err_buf = HeadTailBuffer(self._output_max)
        for item in api.exec_start(ex["Id"], stream=True, demux=True):
            o, e = item if item else (None, None)
            if o:
                out_buf.feed(o)
            if e:
                err_buf.feed(e)
        code = api.exec_inspect(ex["Id"]).get("ExitCode")
        return (
            -1 if code is None else int(code),
            out_buf.getvalue().decode("utf-8", errors="replace"),
            err_buf.getvalue().decode("utf-8", errors="replace"),
        )

    async def _sh(self, argv: Sequence[str], *, cwd: str = "/work") -> tuple[int, str, str]:
        return await asyncio.to_thread(self._exec_sync, argv, cwd=cwd, env=None)

    async def _put(self, dir_path: str, fileobj: Any) -> None:
        await asyncio.to_thread(self._client.api.put_archive, self.name, dir_path, fileobj)

    async def _mkdir(self, path: str) -> None:
        if path in ("/work", paths.HOME):
            return
        code, _, err = await self._sh(["mkdir", "-p", path])
        if code != 0:
            raise RuntimeError(f"cannot create {path}: {err.strip()}")

    async def _get(self, path: str) -> tuple[Any, dict[str, Any]] | None:
        try:
            return await asyncio.to_thread(self._client.api.get_archive, self.name, path)
        except NotFound:
            return None

    # -- exec ------------------------------------------------------------
    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str = paths.REPO,
        env: Mapping[str, str] | None = None,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        self._check()
        prefix = ["timeout", "--kill-after=5s", f"{timeout_s}s"]
        stage: str | None = None
        if stdin is None:
            cmd = [*prefix, *argv]
        else:
            stage = f"{_STAGE_DIR}/{uuid.uuid4().hex}"
            await self._mkdir(_STAGE_DIR)
            await self._put(_STAGE_DIR, _tar_one(stage.rsplit("/", 1)[1], stdin.encode()))
            cmd = [
                "sh",
                "-c",
                'f=$1; shift; "$@" < "$f"; rc=$?; rm -f "$f"; exit $rc',
                "sh",
                stage,
                *prefix,
                *argv,
            ]
        start = time.monotonic()
        try:
            code, out, err = await asyncio.wait_for(
                asyncio.to_thread(self._exec_sync, cmd, cwd=cwd, env=env),
                timeout=timeout_s + 30,
            )
        except TimeoutError:
            await asyncio.to_thread(self._client.api.kill, self.name)
            await asyncio.to_thread(self._client.api.restart, self.name, 5)
            return ExecResult(124, "", "outer exec guard expired", True, time.monotonic() - start)
        dur = time.monotonic() - start
        return ExecResult(code, out, err, code in (124, 137) and dur >= timeout_s, dur)

    # -- files -----------------------------------------------------------
    async def read_bytes(self, path: str) -> bytes | None:
        return await self._read(path, None)

    async def _read(self, path: str, sink: HeadTailBuffer | None) -> bytes | None:
        """Whole file as bytes (sink None, refusing huge files), or streamed into sink."""
        self._check()
        if self._archive_ok(path):
            return await self._read_archive(path, sink)
        stage = f"{_STAGE_DIR}/{uuid.uuid4().hex}"
        code, _, err = await self._sh(
            [
                "sh",
                "-c",
                f'test -f "$1" || exit {_MISSING}; mkdir -p {_STAGE_DIR}; cp "$1" "$2"',
                "sh",
                path,
                stage,
            ]
        )
        if code == _MISSING:
            return None
        if code != 0:
            raise RuntimeError(f"cannot read {path}: {err.strip()}")
        try:
            return await self._read_archive(stage, sink)
        finally:
            await self._sh(["rm", "-f", stage])

    def _read_archive_sync(self, path: str, sink: HeadTailBuffer | None) -> bytes | None:
        try:
            stream, stat = self._client.api.get_archive(self.name, path)
        except NotFound:
            return None
        size = int(stat.get("size", 0))
        if sink is None and size > READ_BYTES_MAX:
            raise SandboxFileTooLarge(path, size)
        with tarfile.open(fileobj=_IterReader(stream), mode="r|") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                f = tf.extractfile(member)
                if f is None:
                    return None
                if sink is None:
                    return f.read()
                while chunk := f.read(1 << 20):
                    sink.feed(chunk)
                return b""
        return None

    async def _read_archive(self, path: str, sink: HeadTailBuffer | None) -> bytes | None:
        return await asyncio.to_thread(self._read_archive_sync, path, sink)

    async def read_text(self, path: str) -> str | None:
        buf = HeadTailBuffer(self._output_max)
        if await self._read(path, buf) is None:
            return None
        return buf.getvalue().decode("utf-8", errors="replace")

    async def write_bytes(self, path: str, content: bytes) -> None:
        self._check()
        parent, base = path.rsplit("/", 1)
        if self._archive_ok(path):
            await self._mkdir(parent)
            await self._put(parent, _tar_one(base, content))
            return
        await self._mkdir(_STAGE_DIR)
        sid = uuid.uuid4().hex
        await self._put(_STAGE_DIR, _tar_one(sid, content))
        code, _, err = await self._sh(
            ["sh", "-c", 'mkdir -p "$1" && mv "$2" "$3"', "sh", parent, f"{_STAGE_DIR}/{sid}", path]
        )
        if code != 0:
            await self._sh(["rm", "-f", f"{_STAGE_DIR}/{sid}"])
            raise RuntimeError(f"cannot write {path}: {err.strip()}")

    async def write_text(self, path: str, content: str) -> None:
        await self.write_bytes(path, content.encode())

    async def remove(self, path: str) -> None:
        self._check()
        await self._sh(["rm", "-f", path])

    # -- large transfers ---------------------------------------------------
    def _spool(self) -> Path:
        self._spool_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self._spool_dir, prefix="xfer-")
        os.close(fd)
        return Path(name)

    def _copy_out_sync(self, path: str, dest: Path, max_bytes: int) -> int:
        try:
            stream, stat = self._client.api.get_archive(self.name, path)
        except NotFound as e:
            raise FileNotFoundError(path) from e
        if int(stat.get("size", 0)) > max_bytes:
            raise TransferTooLarge(max_bytes)
        spool = self._spool()
        try:
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

    async def copy_out(self, path: str, dest: Path, *, max_bytes: int) -> int:
        return await asyncio.to_thread(self._copy_out_sync, path, dest, max_bytes)

    def _copy_in_sync(self, src: Path, path: str) -> None:
        self._check()
        spool = self._spool()
        try:
            with spool.open("wb") as f, tarfile.open(fileobj=f, mode="w") as tf:
                info = tf.gettarinfo(str(src), arcname=path.rsplit("/", 1)[1])
                info.uid = info.gid = _UID
                info.mode = 0o644
                with src.open("rb") as sf:
                    tf.addfile(info, sf)
            with spool.open("rb") as f:
                ok = self._client.api.put_archive(self.name, path.rsplit("/", 1)[0], f)
            if not ok:
                raise RuntimeError(f"cannot copy into {path}")
        finally:
            spool.unlink(missing_ok=True)

    async def copy_in(self, src: Path, path: str) -> None:
        self._check()
        await self._mkdir(path.rsplit("/", 1)[0])
        await asyncio.to_thread(self._copy_in_sync, src, path)

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self._check()
        self._count += 1
        if self._count == 1:
            await asyncio.to_thread(self._client.api.start, self.name)

    async def stop(self) -> None:
        self._count = max(0, self._count - 1)
        if self._count == 0:
            await asyncio.to_thread(self._client.api.stop, self.name, 5)

    async def restart(self) -> None:
        self._check()
        await asyncio.to_thread(self._client.api.restart, self.name, 5)


class DockerSandboxProvider:
    def __init__(
        self,
        client_factory: Callable[[], docker.DockerClient],
        *,
        attach: Sequence[str] = (EDGE_ALIAS,),
        spool_dir: Path | None = None,
        output_max_bytes: int = 2_000_000,
    ) -> None:
        self._factory = client_factory
        self._output_max = output_max_bytes
        self._marks: dict[UUID, int] = {}
        self._client_: docker.DockerClient | None = None
        self._attach = tuple(attach)
        self._boxes: dict[UUID, DockerSandbox] = {}
        self._lock = asyncio.Lock()
        self._spool_dir = spool_dir or (
            Path(os.environ.get("FACTORY_REPOS_DIR", "/repos")) / ".spool"
        )

    @property
    def _client(self) -> docker.DockerClient:
        if self._client_ is None:
            self._client_ = self._factory()
        return self._client_

    # -- sync helpers (run in threads) -----------------------------------
    def _image_digest(self, image: str) -> str:
        attrs = self._client.images.get(image).attrs
        digests = attrs.get("RepoDigests") or []
        return digests[0].split("@", 1)[-1] if digests else str(attrs["Id"])

    def _ensure_network(self, job_id: UUID) -> None:
        name = job_network_name(job_id)
        nets = [n for n in self._client.networks.list(names=[name]) if n.name == name]
        if nets:
            net = nets[0]
        else:
            net = self._client.networks.create(**network_create_args(job_id, _expires(SANDBOX_TTL)))
        net.reload()
        connected = set((net.attrs.get("Containers") or {}).keys())
        for svc in self._attach:
            found = self._client.containers.list(filters={"label": f"{COMPOSE_SERVICE}={svc}"})
            if not found:
                event = "sandbox.edge_missing" if svc == EDGE_ALIAS else "sandbox.mirror_missing"
                log.warning(event, service=svc)
                continue
            if found[0].id in connected:
                continue
            try:
                net.connect(found[0], aliases=[svc])
            except APIError as e:
                if "already exists" not in str(e):
                    raise

    def _make_volume(self, name: str, job_id: UUID, ttl: Any, **extra: str) -> None:
        labels = {
            LABEL_JOB: str(job_id),
            LABEL_MANAGED: "true",
            LABEL_EXPIRES: str(int(_expires(ttl).timestamp())),
            **extra,
        }
        self._client.volumes.create(name=name, labels=labels)

    def _volume_exists(self, name: str) -> bool:
        try:
            self._client.volumes.get(name)
        except NotFound:
            return False
        return True

    def _rm_container(self, name: str) -> None:
        with contextlib.suppress(NotFound):
            self._client.api.remove_container(name, force=True, v=False)

    def _rm_volume(self, name: str) -> None:
        with contextlib.suppress(NotFound):
            self._client.volumes.get(name).remove(force=True)

    def _create_container(self, name: str, cfg: dict[str, Any]) -> None:
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
        self._client.api.start(name)

    def _container_state(self, name: str) -> str | None:
        try:
            return str(self._client.api.inspect_container(name)["State"]["Status"])
        except NotFound:
            return None

    def _ensure_sync(self, job_id: UUID, runner: RunnerSpec) -> DockerSandbox:
        self._ensure_network(job_id)
        cname = container_name(job_id)
        state = self._container_state(cname)
        if state is None:
            for vol in (volume_name(job_id), home_volume_name(job_id)):
                if not self._volume_exists(vol):
                    self._make_volume(vol, job_id, SANDBOX_TTL)
            cfg = container_config(
                job_id,
                runner,
                work=volume_name(job_id),
                home=home_volume_name(job_id),
                network=job_network_name(job_id),
                expires_at=_expires(SANDBOX_TTL),
            )
            self._create_container(cname, cfg)
        elif state != "running" and job_id not in self._marks:
            self._client.api.start(cname)
        return DockerSandbox(
            self._client,
            job_id,
            cname,
            self._image_digest(runner.image),
            home_volume=True,
            spool_dir=self._spool_dir,
            output_max_bytes=self._output_max,
            marks=self._marks,
        )

    # -- workspace limits --------------------------------------------------
    def mark_over_limit(self, job_id: UUID, max_work_gb: int) -> None:
        self._marks[job_id] = max_work_gb

    def _du_kb(self, name: str) -> int:
        api = self._client.api
        ex = api.exec_create(
            name,
            ["timeout", "60", "/usr/bin/du", "-skx", "/work", paths.HOME],
            user=f"{_UID}:{_UID}",
            stdout=True,
            stderr=False,
        )
        out = api.exec_start(ex["Id"])
        total = 0
        for line in (out or b"").decode("utf-8", errors="replace").splitlines():
            first = line.split("\t", 1)[0].strip()
            if first.isdigit():
                total += int(first)
        return total

    def _check_sync(self) -> list[tuple[UUID, int, int]]:
        over: list[tuple[UUID, int, int]] = []
        for c in self._client.containers.list(filters={"label": f"{LABEL_MANAGED}=true"}):
            labels = c.labels or {}
            raw_job, raw_limit = labels.get(LABEL_JOB), labels.get(LABEL_MAX_WORK_GB)
            if not raw_job or not raw_limit:
                continue
            try:
                job_id, limit = UUID(raw_job), int(raw_limit)
                used = self._du_kb(str(c.name))
            except (ValueError, APIError, NotFound):
                continue
            if over_work_limit(used, limit):
                with contextlib.suppress(APIError, NotFound):
                    c.kill()
                self.mark_over_limit(job_id, limit)
                log.warning(
                    "sandbox.over_limit",
                    container=c.name,
                    used_gb=round(used / 2**20, 2),
                    limit_gb=limit,
                )
                over.append((job_id, limit, used))
        return over

    async def check_workspaces(self) -> list[UUID]:
        return [j for j, _, _ in await asyncio.to_thread(self._check_sync)]

    # -- public ----------------------------------------------------------
    async def ensure(self, job_id: UUID, runner: RunnerSpec) -> DockerSandbox:
        async with self._lock:
            box = await asyncio.to_thread(self._ensure_sync, job_id, runner)
            cached = self._boxes.get(job_id)
            if cached is not None:
                return cached
            self._boxes[job_id] = box
            return box

    def _destroy_sync(self, job_id: UUID) -> None:
        client = self._client
        for c in client.containers.list(all=True, filters={"label": f"{LABEL_JOB}={job_id}"}):
            with contextlib.suppress(NotFound):
                c.remove(force=True)
        for n in client.networks.list(names=[job_network_name(job_id)]):
            if n.name != job_network_name(job_id):
                continue
            n.reload()
            for cid in list((n.attrs.get("Containers") or {}).keys()):
                with contextlib.suppress(APIError, NotFound):
                    n.disconnect(cid, force=True)
            with contextlib.suppress(NotFound):
                n.remove()
        for v in client.volumes.list(filters={"label": f"{LABEL_JOB}={job_id}"}):
            with contextlib.suppress(NotFound):
                v.remove(force=True)

    async def destroy(self, job_id: UUID) -> None:
        async with self._lock:
            self._boxes.pop(job_id, None)
            await asyncio.to_thread(self._destroy_sync, job_id)
            self._marks.pop(job_id, None)

    def _reap_sync(self, live: set[UUID], active: set[UUID]) -> int:
        client = self._client
        flt: dict[str, str | list[str] | bool] = {"label": f"{LABEL_MANAGED}=true"}
        found: list[Labelled] = []
        for c in client.containers.list(all=True, filters=flt):
            found.append(Labelled("container", str(c.name), dict(c.labels or {})))
        for n in client.networks.list(filters=flt):
            found.append(
                Labelled("network", str(n.name), dict((n.attrs or {}).get("Labels") or {}))
            )
        for v in client.volumes.list(filters=flt):
            labels = dict((v.attrs or {}).get("Labels") or {})
            found.append(Labelled("volume", str(v.name), labels))
        doomed = select_orphans(found, live, active, datetime.now(UTC))
        removed = 0
        for r in doomed:
            try:
                if r.kind == "container":
                    client.api.remove_container(r.name, force=True, v=False)
                elif r.kind == "network":
                    nets = [n for n in client.networks.list(names=[r.name]) if n.name == r.name]
                    for n in nets:
                        n.reload()
                        for cid in list((n.attrs.get("Containers") or {}).keys()):
                            with contextlib.suppress(APIError, NotFound):
                                n.disconnect(cid, force=True)
                        n.remove()
                else:
                    client.volumes.get(r.name).remove(force=True)
            except NotFound:
                continue
            except APIError as exc:
                log.warning("sandbox.reap_failed", kind=r.kind, name=r.name, error=repr(exc))
                continue
            removed += 1
        return removed

    async def reap_orphans(self, live: set[UUID], active: set[UUID]) -> int:
        async with self._lock:
            removed = await asyncio.to_thread(self._reap_sync, live, active)
            for jid in [j for j in self._boxes if j not in live]:
                del self._boxes[jid]
            return removed

    def _git(self, *args: str) -> list[str]:
        return ["git", *_SETUP_FLAGS, *args]

    def _verify_prepare(self, job_id: UUID, runner: RunnerSpec, name: str, head: str) -> str:
        self._ensure_network(job_id)
        cname, _ = isolated_names(job_id, name)
        self._rm_container(cname)
        vol = verified_volume_name(job_id)
        self._rm_volume(vol)
        self._make_volume(vol, job_id, SANDBOX_TTL, **{LABEL_HEAD: head})
        cfg = container_config(
            job_id,
            runner,
            work=vol,
            home=None,
            network=job_network_name(job_id),
            snapshot=volume_name(job_id),
            expires_at=_expires(EPHEMERAL_TTL),
        )
        self._create_container(cname, cfg)
        return cname

    @asynccontextmanager
    async def verify_box(
        self, job_id: UUID, runner: RunnerSpec, *, name: str, head: str
    ) -> AsyncIterator[DockerSandbox]:
        async with self._lock:
            cname = await asyncio.to_thread(self._verify_prepare, job_id, runner, name, head)
        box = DockerSandbox(
            self._client,
            job_id,
            cname,
            await asyncio.to_thread(self._image_digest, runner.image),
            home_volume=False,
            spool_dir=self._spool_dir,
            output_max_bytes=self._output_max,
            marks=self._marks,
        )
        try:
            ok = await self._verify_setup(box, head)
            if not ok:
                await asyncio.to_thread(self._rm_container, cname)
                await asyncio.to_thread(self._rm_volume, verified_volume_name(job_id))
                raise RuntimeError(f"verify box head is not {head}")
            yield box
        finally:
            await asyncio.to_thread(self._rm_container, cname)

    async def _verify_setup(self, box: DockerSandbox, head: str) -> bool:
        env = dict(SAFE_GIT_ENV)
        steps = [
            self._git("init", "-q", "/work/repo"),
            self._git(
                "-C",
                "/work/repo",
                "fetch",
                "-q",
                "--no-tags",
                "file:///snapshot/repo",
                "+refs/heads/*:refs/remotes/job/*",
            ),
            self._git("-C", "/work/repo", "checkout", "-q", "--detach", head),
        ]
        for argv in steps:
            r = await box.exec(argv, cwd="/work", env=env, timeout_s=600)
            if r.exit_code != 0:
                return False
        r = await box.exec(self._git("rev-parse", "HEAD"), cwd="/work/repo", env=env)
        if r.exit_code != 0 or r.stdout.strip() != head:
            return False
        r = await box.exec(["mkdir", "-p", "/work/.factory/in", "/work/.factory/out"], cwd="/work")
        return r.exit_code == 0

    def _isolated_prepare(self, job_id: UUID, runner: RunnerSpec, name: str) -> tuple[str, str]:
        if not self._volume_exists(verified_volume_name(job_id)):
            raise RuntimeError(f"no verified tree for job {job_id}")
        self._ensure_network(job_id)
        cname, vol = isolated_names(job_id, name)
        self._rm_container(cname)
        self._rm_volume(vol)
        self._make_volume(vol, job_id, EPHEMERAL_TTL)
        cfg = container_config(
            job_id,
            runner,
            work=vol,
            home=None,
            network=job_network_name(job_id),
            snapshot=verified_volume_name(job_id),
            expires_at=_expires(EPHEMERAL_TTL),
        )
        self._create_container(cname, cfg)
        return cname, vol

    @asynccontextmanager
    async def isolated(
        self, job_id: UUID, runner: RunnerSpec, *, name: str
    ) -> AsyncIterator[DockerSandbox]:
        async with self._lock:
            cname, vol = await asyncio.to_thread(self._isolated_prepare, job_id, runner, name)
        try:
            box = DockerSandbox(
                self._client,
                job_id,
                cname,
                await asyncio.to_thread(self._image_digest, runner.image),
                home_volume=False,
                spool_dir=self._spool_dir,
                output_max_bytes=self._output_max,
                marks=self._marks,
            )
            r = await box.exec(
                ["sh", "-c", _ISOLATED_SETUP, "sh", ISOLATED_GIT_CONFIG],
                cwd="/work",
                env=dict(SAFE_GIT_ENV),
                timeout_s=600,
            )
            if r.exit_code != 0:
                raise RuntimeError(f"isolated setup failed: {r.stderr.strip()}")
            yield box
        finally:
            await asyncio.to_thread(self._rm_container, cname)
            await asyncio.to_thread(self._rm_volume, vol)
