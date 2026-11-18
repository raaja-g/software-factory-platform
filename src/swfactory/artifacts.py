"""Minimal artifact store: text artifacts in app.artifacts.content (M3.5 extends it)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swfactory.db.tables import Artifact
from swfactory.sandbox.base import Sandbox
from swfactory.sandbox.paths import IN


@dataclass(frozen=True, slots=True)
class ArtifactMeta:
    path: str
    kind: str
    sha256: str
    size: int
    run_id: UUID | None
    created_at: datetime


_LATEST = (
    "SELECT path, kind, sha256, run_id, created_at, content FROM app.artifacts_latest "
    "WHERE job_id = :j"
)


class ArtifactStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def put(
        self,
        job_id: UUID,
        path: str,
        content: str | bytes,
        *,
        kind: str = "md",
        run_id: UUID | None = None,
    ) -> ArtifactMeta:
        body = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        digest = hashlib.sha256(body.encode()).hexdigest()
        async with self.sessions() as s, s.begin():
            row = Artifact(
                job_id=job_id, path=path, kind=kind, content=body, sha256=digest, run_id=run_id
            )
            s.add(row)
            await s.flush()
            await s.refresh(row)
            created = row.created_at
        return ArtifactMeta(path, kind, digest, len(body.encode()), run_id, created)

    async def get_text(self, job_id: UUID, path: str) -> str | None:
        async with self.sessions() as s:
            return await s.scalar(
                text("SELECT content FROM app.artifacts_latest WHERE job_id = :j AND path = :p"),
                {"j": job_id, "p": path},
            )

    async def list(self, job_id: UUID) -> list[ArtifactMeta]:
        async with self.sessions() as s:
            rows = (await s.execute(text(_LATEST + " ORDER BY path"), {"j": job_id})).all()
        return [
            ArtifactMeta(
                r.path,
                r.kind,
                r.sha256 or "",
                len((r.content or "").encode()),
                r.run_id,
                r.created_at,
            )
            for r in rows
        ]

    async def materialize(
        self, job_id: UUID, box: Sandbox, paths: Sequence[str] | None = None
    ) -> None:
        async with self.sessions() as s:
            rows = (await s.execute(text(_LATEST), {"j": job_id})).all()
        wanted = None if paths is None else set(paths)
        for r in rows:
            if r.content is None or (wanted is not None and r.path not in wanted):
                continue
            await box.write_text(f"{IN}/{r.path}", r.content)
