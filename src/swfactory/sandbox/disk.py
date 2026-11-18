"""Disk and output limits: head/tail output capping, filesystem usage and workspace size checks."""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swfactory.settings import Settings

WORKSPACE_CHECK_S = 60


class HeadTailBuffer:
    """Keeps the first and last max_bytes // 2 bytes of a stream and counts what it dropped."""

    def __init__(self, max_bytes: int) -> None:
        self._half = max_bytes // 2
        self._head = bytearray()
        self._tail = bytearray()
        self.omitted = 0

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        room = self._half - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self._tail += chunk
        excess = len(self._tail) - self._half
        if excess > 0:
            del self._tail[:excess]
            self.omitted += excess

    def getvalue(self) -> bytes:
        if self.omitted == 0:
            return bytes(self._head) + bytes(self._tail)
        return (
            bytes(self._head) + f"\n[omitted: {self.omitted} bytes]\n".encode() + bytes(self._tail)
        )


def disk_usage_pct(paths: Sequence[str]) -> tuple[int, str]:
    """The fullest path's used percent (rounded up) and that path."""
    best = (0, "/")
    for p in paths:
        st = os.statvfs(p)
        if st.f_blocks <= 0:
            continue
        pct = math.ceil(100 * (st.f_blocks - st.f_bavail) / st.f_blocks)
        if pct > best[0] or not best[0]:
            best = (pct, p)
    return best


def disk_paths(settings: Settings) -> tuple[str, ...]:
    return tuple(
        p for p in ("/", settings.repos_dir, str(settings.artifacts_dir)) if os.path.isdir(p)
    )


def claim_new_allowed(pct: int, high_water_pct: int) -> bool:
    return pct <= high_water_pct


def over_work_limit(used_kb: int, max_work_gb: int) -> bool:
    return used_kb > max_work_gb * 2**20
