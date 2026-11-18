from __future__ import annotations

from typing import Any

from langgraph.types import Command, interrupt
from sqlalchemy import func, select

from swfactory.db.tables import JobEvent
from swfactory.domain.types import HumanAction, Stage
from swfactory.events import record_event
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import job_uuid, merged
from swfactory.graph.routing import (
    gate_artifacts,
    route_after_human,
    route_requirements_gate,
    transition,
)
from swfactory.graph.state import JobState


def make(deps: Deps):
    async def _emit_action_once(state: JobState, human: dict[str, Any]) -> None:
        """Emit human.action unless this resume payload was already recorded (a re-run)."""
        data = {
            "action": human.get("action"),
            "by": human.get("by"),
            "command_id": human.get("command_id"),
            "status": "applied",
        }
        job_id = job_uuid(state)
        async with deps.sessions.begin() as s:
            since = await s.scalar(
                select(func.max(JobEvent.id)).where(
                    JobEvent.job_id == job_id, JobEvent.kind == "human.waiting"
                )
            )
            seen = await s.scalars(
                select(JobEvent.data).where(
                    JobEvent.job_id == job_id,
                    JobEvent.kind == "human.action",
                    JobEvent.id > (since or 0),
                )
            )
            if data not in list(seen):
                await record_event(s, job_id, "human.action", data)

    def _requirements_artifacts(state: JobState) -> list[str]:
        reason = state.get("gate_reason") or ""
        paths = ["requirements.md"]
        if reason.startswith("step failed"):
            paths.append("requirements.invalid.md")
        return paths

    def _requirements_update(new: JobState, human: dict[str, Any]) -> dict[str, Any]:
        action = human.get("action")
        update: dict[str, Any] = {"gate_reason": None}
        if action == HumanAction.APPROVE.value:
            update.update(outcome=Stage.MERGED.value, approved_by=human.get("by"), retry_node=None)
        elif action in (HumanAction.REJECT.value, HumanAction.CANCEL.value):
            update.update(outcome=Stage.CLOSED.value, retry_node=None)
        elif action == HumanAction.REWORK.value:  # not limited to max_rounds
            update.update(round=new.get("round", 1) + 1, retry_node=None)
        return update

    async def human_gate(state: JobState) -> Command:
        is_req = state.get("kind") == "requirements"
        value = interrupt(
            {
                "reason": state.get("gate_reason"),
                "retry_node": state.get("retry_node"),
                "artifacts": _requirements_artifacts(state) if is_req else gate_artifacts(state),
            }
        )
        human: dict[str, Any] = dict(value)
        if human.get("action") == HumanAction.REWORK.value:
            path = f"rework-{state.get('round', 1) + 1}.md"
            note = human.get("note") or ""
            if await deps.artifacts.get_text(job_uuid(state), path) != note:
                await deps.artifacts.put(job_uuid(state), path, note)
        await _emit_action_once(state, human)
        update: dict = {"human": human}
        new = merged(state, update)
        if is_req:
            target = route_requirements_gate(new)
            update.update(_requirements_update(new, human))
            return Command(goto=target, update=update)
        target = route_after_human(new)
        update.update(transition(new, "human_gate", target))
        return Command(goto=target, update=update)

    return human_gate
