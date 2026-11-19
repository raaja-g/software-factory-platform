"""Human actions: which stages accept them and how they resume a waiting graph."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langgraph.types import Command

from swfactory.domain.types import ACTIVE_STAGES, TERMINAL_STAGES, HumanAction, Stage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class CommandRefused(Exception):
    """A human action that cannot be applied to the job as it is now."""


GATE_ACTIONS = frozenset(
    {
        HumanAction.APPROVE,
        HumanAction.REWORK,
        HumanAction.REJECT,
        HumanAction.RETRY,
        HumanAction.ADD_BUDGET,
    }
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
    head_sha: str | None = None,
) -> Command | None:
    """The graph resume for a gate action; None for cancel (the worker stops the job)."""
    if action == HumanAction.CANCEL:
        return None
    if action not in GATE_ACTIONS:
        raise CommandRefused(f"{action.value} is not a gate action")
    if action == HumanAction.REWORK and not (note or "").strip():
        raise CommandRefused("rework needs a note")
    if action in (HumanAction.RETRY, HumanAction.ADD_BUDGET) and not retry_offered:
        raise CommandRefused("retry not offered")
    return Command(
        resume={
            "action": action.value,
            "note": (note or "").strip(),
            "by": by,
            "command_id": str(command_id) if command_id else "",
            "head_sha": head_sha,
        }
    )


_REF = re.compile(r"^([^:/#\s]+)[:/#](\d+)$")


def parse_job_ref(ref: str) -> UUID | tuple[str, int]:
    """A uuid or ``<app>:<seq>`` (also ``<app>/3``, ``<app>#003``); else ValueError."""
    ref = ref.strip()
    m = _REF.match(ref)
    if m is not None:
        return m.group(1), int(m.group(2))
    return UUID(ref)


def format_job_ref(app_slug: str, seq: int) -> str:
    return f"{app_slug}:{seq:03d}"


def retry_offered(waiting: Mapping[str, Any] | None) -> bool:
    """Whether the graph offered a retry at the latest wait (its ``retry_node`` is set)."""
    return waiting is not None and waiting.get("retry_node") is not None


def action_allowed(action: HumanAction, *, stage: Stage, retry_offered: bool) -> bool:
    if action is HumanAction.CANCEL:
        return stage in ACTIVE_STAGES or stage in {
            Stage.QUEUED,
            Stage.NEEDS_HUMAN,
            Stage.FAILED,
            Stage.DELIVERED,
        }
    if action is HumanAction.ADD_BUDGET:
        return stage is Stage.NEEDS_HUMAN and retry_offered
    if action is HumanAction.RETRY:
        return stage is Stage.FAILED or (stage is Stage.NEEDS_HUMAN and retry_offered)
    if action in GATE_ACTIONS:
        return stage is Stage.NEEDS_HUMAN
    return False


async def submit_command(
    s: AsyncSession,
    *,
    job_id: UUID,
    action: HumanAction,
    note: str | None,
    requested_by: str,
    head_sha: str | None = None,
    source_ref: str | None = None,
    amount_usd: Decimal | None = None,
) -> UUID:
    """The single entry point for human actions (API, CLI, PR comments); never commits."""
    from swfactory.db.repo import (
        command_by_source_ref,
        insert_command,
        latest_waiting,
        lock_job,
    )
    from swfactory.db.tables import Job
    from swfactory.graph.build import GRAPH_VERSION

    if action is not HumanAction.ADD_BUDGET and amount_usd is not None:
        raise CommandRefused("only add_budget takes an amount")
    if action is HumanAction.ADD_BUDGET and (amount_usd is None or amount_usd <= 0):
        raise CommandRefused("add_budget needs an amount above zero")
    await lock_job(s, job_id)
    if source_ref is not None:  # a repeated comment is the same command, never a second row
        existing = await command_by_source_ref(s, source_ref)
        if existing is not None:
            return existing
    job = await s.get(Job, job_id, populate_existing=True)
    if job is None:
        raise CommandRefused("no such job")
    stage = Stage(job.stage)
    check_command_allowed(action, stage, note)
    waiting = await latest_waiting(s, job_id) if stage is Stage.NEEDS_HUMAN else None
    wdata = None if waiting is None else waiting[1]
    if action is HumanAction.ADD_BUDGET and (wdata is None or wdata.get("reason") != "budget"):
        raise CommandRefused("only a budget stop can take more budget")
    if not action_allowed(action, stage=stage, retry_offered=retry_offered(wdata)):
        if action in (HumanAction.RETRY, HumanAction.ADD_BUDGET):
            raise CommandRefused("retry not offered")
        raise CommandRefused("job is not waiting for a person")
    if (
        action is HumanAction.RETRY
        and stage is Stage.FAILED
        and job.graph_version is not None
        and job.graph_version != str(GRAPH_VERSION)
    ):
        raise CommandRefused("graph version no longer supported")
    if action is HumanAction.ADD_BUDGET:
        assert amount_usd is not None
        job_max = await _job_max_usd(s, job)
        if job.budget_usd + amount_usd > job_max:
            raise CommandRefused(f"the job budget can't go above ${job_max}")
    wait_id: int | None = None
    if action in GATE_ACTIONS and stage is Stage.NEEDS_HUMAN and waiting is not None:
        from swfactory.db.repo import pending_gate_command

        wait_id = waiting[0]
        if await pending_gate_command(s, job_id, wait_id) is not None:
            raise CommandRefused("an action is already pending for this wait")
    if action is HumanAction.APPROVE:
        waiting_sha = None if wdata is None else wdata.get("head_sha")
        if head_sha is not None and waiting_sha is not None and head_sha != waiting_sha:
            raise CommandRefused("the branch moved; reload")
        if head_sha is None:
            head_sha = waiting_sha
    return await insert_command(
        s,
        job_id=job_id,
        action=action.value,
        note=note,
        requested_by=requested_by,
        head_sha=head_sha,
        gate_event_id=wait_id,
        amount_usd=amount_usd,
        source_ref=source_ref,
    )


async def _job_max_usd(s: AsyncSession, job: Any) -> Decimal:
    """``budgets.job_max_usd`` of the job's pinned definition (the default when it has none)."""
    from swfactory.db.tables import DefinitionRow
    from swfactory.definition.models import Budgets

    content = None
    if job.definition_sha is not None:
        row = await s.get(DefinitionRow, job.definition_sha)
        content = None if row is None else row.content
    raw = ((content or {}).get("factory") or {}).get("budgets") or {}
    value = raw.get("job_max_usd")
    return Decimal(str(value)) if value is not None else Budgets().job_max_usd
