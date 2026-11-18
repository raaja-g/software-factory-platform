"""Job graph state and the reviews reducer."""

from typing import Annotated, NotRequired, TypedDict


class ReviewOutcome(TypedDict):
    kind: str
    verdict: str
    artifact: str
    run_id: str
    diff_cut: NotRequired[bool]  # the reviewer's prompt cut diff.patch (M3.8)


class ReviewTask(TypedDict):
    job_id: str
    round: int
    kind: str
    screenshots: NotRequired[list[str]]
    fan_out: NotRequired[int]  # reviewers started in this round (budget share)


def merge_reviews(
    left: dict[str, dict[str, ReviewOutcome]] | None,
    right: dict[str, dict[str, ReviewOutcome]] | None,
) -> dict[str, dict[str, ReviewOutcome]]:
    """Merge per round and kind; the right side wins. Never mutates its inputs."""
    merged: dict[str, dict[str, ReviewOutcome]] = {r: dict(k) for r, k in (left or {}).items()}
    for round_key, kinds in (right or {}).items():
        merged.setdefault(round_key, {}).update(kinds)
    return merged


class JobState(TypedDict, total=False):
    job_id: str
    kind: str
    bench_reference: bool
    round: int
    max_rounds: int
    fix_attempts: int
    max_fix_attempts: int
    verify_status: str
    verify_reason: str | None
    screenshots: list[str]
    approved_head: str | None
    reviewers: list[str]
    reviews: Annotated[dict[str, dict[str, ReviewOutcome]], merge_reviews]
    decision: str | None
    diff_cut: bool
    require_human_approval: bool
    approval_from_job: bool
    gate_reason: str | None
    gate_scope: str | None
    retry_node: str | None
    human: dict[str, str] | None
    outcome: str | None
    approved_by: str | None
