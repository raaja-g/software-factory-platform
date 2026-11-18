"""Graph nodes: each module has ``make(deps)`` returning the async node function."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any
from uuid import UUID

from swfactory.db.repo import LeaseLost, holds_lease, set_stage
from swfactory.domain.types import Stage
from swfactory.graph.deps import Deps
from swfactory.graph.state import JobState

# (worker_id, lease_epoch) of the claim driving the running node; set by build_job_graph's
# node wrapper when the graph runs under a worker, unset in the graph-only tests
LEASE: ContextVar[tuple[str, int] | None] = ContextVar("swfactory_lease", default=None)


def job_uuid(state: JobState) -> UUID:
    return UUID(str(state.get("job_id")))


async def enter_stage(deps: Deps, job_id: UUID, stage: Stage, **fields: Any) -> bool:
    """Every stage write of a node goes through here (and so through ``set_stage``)."""
    async with deps.sessions.begin() as s:
        lease = LEASE.get()
        if lease is not None and not await holds_lease(
            s, job_id=job_id, worker_id=lease[0], lease_epoch=lease[1]
        ):
            raise LeaseLost(str(job_id))
        return await set_stage(s, job_id, stage, **fields)


def merged(state: JobState, update: dict[str, Any]) -> JobState:
    """The state as it will be once ``update`` is applied (plain keys only, no reducers)."""
    return {**state, **update}  # type: ignore[return-value]


def build_inputs(state: JobState, *, reviews: bool = True) -> list[str]:
    """Artifact names a step may read; ``materialize`` skips the ones that do not exist."""
    rnd = state.get("round", 1)
    names = ["feature.md", "app.md", "requirements.md", "spec.md", "build.md", "verify.md"]
    for r in range(1, rnd + 1):
        names.append(f"rework-{r}.md")
    if reviews:
        from swfactory.domain.types import DEFAULT_REVIEWERS

        for r in range(1, rnd + 1):
            names += [f"round-{r}/review-{k}.md" for k in DEFAULT_REVIEWERS]
    return names


from swfactory.graph.nodes import (  # noqa: E402
    after_reviews,
    approve,
    build,
    deliver,
    finalize,
    human_gate,
    prepare,
    review,
    score,
    spec,
    triage,
    verify,
)

__all__ = [
    "after_reviews",
    "approve",
    "build",
    "deliver",
    "finalize",
    "human_gate",
    "prepare",
    "review",
    "score",
    "spec",
    "triage",
    "verify",
]
