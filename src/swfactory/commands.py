"""Human actions: which stages accept them and how they resume a waiting graph."""

from __future__ import annotations

from uuid import UUID

from langgraph.types import Command

from swfactory.domain.types import TERMINAL_STAGES, HumanAction, Stage


class CommandRefused(Exception):
    """A human action that cannot be applied to the job as it is now."""


GATE_ACTIONS = frozenset(
    {HumanAction.APPROVE, HumanAction.REWORK, HumanAction.REJECT, HumanAction.RETRY}
)


def check_command_allowed(action: HumanAction, stage: Stage, note: str | None) -> None:
    if action in GATE_ACTIONS:
        if action == HumanAction.RETRY and stage == Stage.FAILED:
            return
        if stage != Stage.NEEDS_HUMAN:
            raise CommandRefused("job is not waiting for a person")
    elif action == HumanAction.CANCEL:
        if stage in TERMINAL_STAGES:
            raise CommandRefused("job has already finished")
    if action == HumanAction.REWORK and not (note or "").strip():
        raise CommandRefused("rework needs a note")


def to_resume(
    action: HumanAction,
    *,
    note: str | None,
    by: str,
    retry_offered: bool,
    command_id: UUID | None = None,
) -> Command | None:
    """The graph resume for a gate action; None for cancel (the worker stops the job)."""
    if action == HumanAction.CANCEL:
        return None
    if action not in GATE_ACTIONS:
        raise CommandRefused(f"{action.value} is not a gate action")
    if action == HumanAction.REWORK and not (note or "").strip():
        raise CommandRefused("rework needs a note")
    if action == HumanAction.RETRY and not retry_offered:
        raise CommandRefused("retry not offered")
    return Command(
        resume={
            "action": action.value,
            "note": (note or "").strip(),
            "by": by,
            "command_id": str(command_id) if command_id else "",
        }
    )
