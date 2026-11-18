"""In-memory ArtifactStore with the same interface, for no-db unit tests."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from swfactory.artifacts import ArtifactMeta
from swfactory.sandbox.base import Sandbox
from swfactory.sandbox.paths import IN


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, str], tuple[ArtifactMeta, str]] = {}

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
        meta = ArtifactMeta(
            path,
            kind,
            hashlib.sha256(body.encode()).hexdigest(),
            len(body.encode()),
            run_id,
            datetime.now(UTC),
        )
        self._rows[(job_id, path)] = (meta, body)
        return meta

    async def get_text(self, job_id: UUID, path: str) -> str | None:
        row = self._rows.get((job_id, path))
        return None if row is None else row[1]

    async def list(self, job_id: UUID) -> list[ArtifactMeta]:
        return sorted(
            (m for (j, _), (m, _) in self._rows.items() if j == job_id), key=lambda m: m.path
        )

    async def materialize(
        self, job_id: UUID, box: Sandbox, paths: Sequence[str] | None = None
    ) -> None:
        for (j, p), (_, body) in self._rows.items():
            if j == job_id and (paths is None or p in paths):
                await box.write_text(f"{IN}/{p}", body)
