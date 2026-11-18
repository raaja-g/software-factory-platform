from __future__ import annotations

from typing import Any

from langgraph.types import Command, interrupt
from sqlalchemy import func, select

from swfactory.db.tables import JobEvent
from swfactory.domain.types import HumanAction
from swfactory.events import record_event
from swfactory.graph.deps import Deps
from swfactory.graph.nodes import job_uuid, merged
from swfactory.graph.routing import gate_artifacts, route_after_human, transition
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

    async def human_gate(state: JobState) -> Command:
        value = interrupt(
            {
                "reason": state.get("gate_reason"),
                "retry_node": state.get("retry_node"),
                "artifacts": gate_artifacts(state),
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
        target = route_after_human(new)
        update.update(transition(new, "human_gate", target))
        return Command(goto=target, update=update)

    return human_gate
