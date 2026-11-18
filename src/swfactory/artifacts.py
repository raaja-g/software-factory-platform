"""Artifact store: small text in app.artifacts.content, large or binary content on a volume."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.db.tables import Artifact
from swfactory.logging import redact
from swfactory.sandbox.base import Sandbox
from swfactory.sandbox.paths import IN

MAX_DB_BYTES = 1_048_576


def validate_artifact_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError(f"invalid artifact path: {path!r}")
    if any(seg in ("..", "") for seg in path.split("/")):
        raise ValueError(f"invalid artifact path: {path!r}")
    return path


@dataclass(frozen=True, slots=True)
class ArtifactMeta:
    path: str
    kind: str
    sha256: str
    size: int
    run_id: UUID | None
    created_at: datetime


_LATEST = (
    "SELECT path, kind, sha256, run_id, created_at, content, storage_uri, "
    "octet_length(content) AS clen FROM app.artifacts_latest WHERE job_id = :j"
)


def _write_file(root: Path, job_id: UUID, digest: str, data: bytes) -> str:
    d = root / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    target = d / digest
    if not target.exists():
        tmp = d / f".{digest}.{uuid.uuid4().hex}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return target.resolve().as_uri()


def _read_file(uri: str) -> bytes | None:
    path = Path(uri.removeprefix("file://"))
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _size(r: Any) -> int:
    if r.storage_uri:
        try:
            return Path(r.storage_uri.removeprefix("file://")).stat().st_size
        except FileNotFoundError:
            return 0
    return r.clen or 0


class ArtifactStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], root: Path) -> None:
        self.sessions = sessions
        self.root = Path(root)

    async def put(
        self,
        job_id: UUID,
        path: str,
        content: str | bytes,
        *,
        kind: str = "md",
        run_id: UUID | None = None,
    ) -> ArtifactMeta:
        validate_artifact_path(path)
        if isinstance(content, bytes):
            data, body = content, None
        else:
            body = redact(content)
            data = body.encode()
        digest = hashlib.sha256(data).hexdigest()
        async with self.sessions() as s:
            latest = (
                await s.execute(
                    text(
                        "SELECT path, kind, sha256, run_id, created_at, content, storage_uri, "
                        "octet_length(content) AS clen FROM app.artifacts_latest "
                        "WHERE job_id = :j AND path = :p"
                    ),
                    {"j": job_id, "p": path},
                )
            ).first()
        if latest is not None and latest.sha256 == digest:
            return ArtifactMeta(
                path, latest.kind, digest, _size(latest), latest.run_id, latest.created_at
            )
        uri: str | None = None
        stored: str | None = None
        if body is not None and len(data) <= MAX_DB_BYTES:
            stored = body
        else:
            uri = await asyncio.to_thread(_write_file, self.root, job_id, digest, data)
        async with self.sessions() as s, s.begin():
            row = Artifact(
                job_id=job_id,
                path=path,
                kind=kind,
                content=stored,
                storage_uri=uri,
                sha256=digest,
                run_id=run_id,
            )
            s.add(row)
            await s.flush()
            await s.refresh(row)
            created = row.created_at
        return ArtifactMeta(path, kind, digest, len(data), run_id, created)

    async def _latest_row(self, job_id: UUID, path: str) -> Any:
        async with self.sessions() as s:
            return (
                await s.execute(
                    text(_LATEST + " AND path = :p"),
                    {"j": job_id, "p": path},
                )
            ).first()

    async def get_text(self, job_id: UUID, path: str) -> str | None:
        r = await self._latest_row(job_id, path)
        if r is None or r.kind == "image":
            return None
        if r.storage_uri:
            data = await asyncio.to_thread(_read_file, r.storage_uri)
            return None if data is None else data.decode("utf-8", errors="replace")
        return r.content

    async def get_bytes(self, job_id: UUID, path: str) -> bytes | None:
        r = await self._latest_row(job_id, path)
        if r is None:
            return None
        if r.storage_uri:
            return await asyncio.to_thread(_read_file, r.storage_uri)
        return None if r.content is None else r.content.encode()

    async def list(self, job_id: UUID) -> list[ArtifactMeta]:
        async with self.sessions() as s:
            rows = (await s.execute(text(_LATEST + " ORDER BY path"), {"j": job_id})).all()
        return [
            ArtifactMeta(r.path, r.kind, r.sha256 or "", _size(r), r.run_id, r.created_at)
            for r in rows
        ]

    async def materialize(
        self, job_id: UUID, box: Sandbox, paths: Sequence[str] | None = None
    ) -> None:
        async with self.sessions() as s:
            rows = (await s.execute(text(_LATEST), {"j": job_id})).all()
        wanted = None if paths is None else set(paths)
        for r in rows:
            if r.kind == "image" or (wanted is not None and r.path not in wanted):
                continue
            if r.storage_uri:
                data = await asyncio.to_thread(_read_file, r.storage_uri)
                if data is None:
                    continue
                await box.write_text(f"{IN}/{r.path}", data.decode("utf-8", errors="replace"))
            elif r.content is not None:
                await box.write_text(f"{IN}/{r.path}", r.content)
