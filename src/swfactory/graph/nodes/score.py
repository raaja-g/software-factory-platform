from __future__ import annotations

from swfactory.graph.deps import Deps
from swfactory.graph.state import JobState


def make(deps: Deps):
    async def score(state: JobState) -> dict:
        return {}  # M10

    return score
