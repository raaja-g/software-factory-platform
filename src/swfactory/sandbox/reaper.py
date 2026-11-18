"""Sandbox reaper: pick labelled Docker resources that no longer belong to a live job."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import select

from swfactory.db.tables import Job
from swfactory.domain.types import ACTIVE_STAGES, TERMINAL_STAGES
from swfactory.sandbox.template import LABEL_EPHEMERAL, LABEL_EXPIRES, LABEL_JOB

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from swfactory.sandbox.base import SandboxProvider

REAP_INTERVAL_S = 600
_ORDER = {"container": 0, "network": 1, "volume": 2}


@dataclass(frozen=True, slots=True)
class Labelled:
    kind: Literal["container", "network", "volume"]
    name: str
    labels: Mapping[str, str]


def _parse(r: Labelled) -> tuple[UUID, datetime] | None:
    try:
        job = UUID(r.labels[LABEL_JOB])
        expires = datetime.fromtimestamp(int(r.labels[LABEL_EXPIRES]), UTC)
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return None
    return job, expires


def select_orphans(
    resources: Sequence[Labelled], live: set[UUID], active: set[UUID], now: datetime
) -> list[Labelled]:
    """The resources to remove: containers first, then networks, then volumes."""
    out: list[Labelled] = []
    for r in resources:
        parsed = _parse(r)
        if parsed is None:
            continue
        job, expires = parsed
        expired = expires <= now
        if r.labels.get(LABEL_EPHEMERAL) == "true":
            reap = job not in active or expired
        elif job in active:
            reap = False
        elif job not in live:
            reap = True
        else:  # a waiting job keeps its network and volumes; only the container expires
            reap = r.kind == "container" and expired
        if reap:
            out.append(r)
    return sorted(out, key=lambda r: _ORDER[r.kind])  # stable within a kind


async def _load(s: AsyncSession) -> tuple[set[UUID], set[UUID]]:
    terminal = {st.value for st in TERMINAL_STAGES}
    active = {st.value for st in ACTIVE_STAGES}
    rows = (await s.execute(select(Job.id, Job.stage))).all()
    live = {jid for jid, stage in rows if stage not in terminal}
    return live, {jid for jid, stage in rows if stage in active}


async def reconcile(provider: SandboxProvider, sessions: async_sessionmaker[AsyncSession]) -> int:
    """Reap sandbox resources of jobs that are gone or terminal; returns how many were removed."""
    async with sessions() as s:
        live, active = await _load(s)
    return await provider.reap_orphans(live, active)
