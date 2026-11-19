"""Budget top-up at a budget stop: refusals, the worker's once-only apply and the resume."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from tests.fakes.deps import seed_demo
from tests.integration import test_worker_commands as twc
from tests.support.db_helpers import enq

from swfactory.commands import CommandRefused, submit_command
from swfactory.db.repo import apply_budget_top_up
from swfactory.db.tables import Command, Job
from swfactory.domain.types import HumanAction

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parents[1] / "fixtures" / "fake"
_bind = twc._bind  # the worker harness of test_worker_commands (autouse binding)


async def job_row() -> Job:
    return await twc.job()


async def set_budget(usd: str) -> None:
    async with twc.ctx.sessions.begin() as s:
        await s.execute(text("update app.jobs set budget_usd = :b"), {"b": Decimal(usd)})


@pytest.fixture
async def budget_job():
    """A job parked at a budget stop: its budget is below the first builder step's minimum."""
    await seed_demo(twc.ctx.sessions, defaults=twc.FAST)
    await enq(twc.ctx.sessions, app="demo")
    await set_budget("0.000001")
    await twc.work("happy.yaml")
    j = await job_row()
    assert (j.stage, j.gate_reason) == ("needs_human", "budget")
    return j.id


@pytest.fixture
async def escalated_job():
    await seed_demo(twc.ctx.sessions, defaults=twc.FAST)
    await enq(twc.ctx.sessions, app="demo")
    await twc.work("escalate.yaml")
    j = await job_row()
    assert (j.stage, j.gate_reason) == ("needs_human", "approver escalated")
    return j.id


async def submit(job_id, action, amount: str | None, **kw: Any):
    async with twc.ctx.sessions.begin() as s:
        return await submit_command(
            s,
            job_id=job_id,
            action=action,
            note=None,
            requested_by="ad@x.io",
            amount_usd=None if amount is None else Decimal(amount),
            **kw,
        )


async def test_refused_unless_budget_stop(escalated_job):
    with pytest.raises(CommandRefused, match="only a budget stop"):
        await submit(escalated_job, HumanAction.ADD_BUDGET, "5")


async def test_refused_above_job_max(budget_job):
    await set_budget("10")
    with pytest.raises(CommandRefused, match="can't go above"):
        await submit(budget_job, HumanAction.ADD_BUDGET, "16")


async def test_amount_only_for_add_budget(budget_job):
    with pytest.raises(CommandRefused, match="only add_budget takes an amount"):
        await submit(budget_job, HumanAction.RETRY, "5")


async def test_add_budget_needs_an_amount(budget_job):
    with pytest.raises(CommandRefused, match="needs an amount"):
        await submit(budget_job, HumanAction.ADD_BUDGET, None)


async def test_top_up_raises_budget_and_reruns_build(budget_job):
    await set_budget("10")  # the stop is parked; give the top-up a known base
    cid = await submit(budget_job, HumanAction.ADD_BUDGET, "5")
    await twc.work("happy.yaml")
    assert (await job_row()).budget_usd == Decimal("15")
    assert (await twc.cmd(cid)).result == "resumed"
    assert [r.status for r in await twc.rows("builder")][-1] == "ok"
    async with twc.ctx.sessions() as s:
        row = (await s.execute(select(Command.amount_usd).where(Command.id == cid))).scalar_one()
    assert row == Decimal("5")


async def test_top_up_applied_once(budget_job):
    await set_budget("10")
    async with twc.ctx.sessions.begin() as s:
        cid = await twc.insert_command(
            s,
            job_id=budget_job,
            action="add_budget",
            note=None,
            requested_by="ad@x.io",
            amount_usd=Decimal("5"),
        )

    async def apply() -> Decimal | None:
        async with twc.ctx.sessions.begin() as s:
            return await apply_budget_top_up(
                s,
                command_id=cid,
                job_id=budget_job,
                default_usd=Decimal("10"),
                job_max_usd=Decimal("25"),
            )

    assert await apply() == Decimal("15")
    assert await apply() is None
    assert (await job_row()).budget_usd == Decimal("15")


async def test_check_constraint_amount_only_on_add_budget(budget_job):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with twc.ctx.sessions.begin() as s:
            await twc.insert_command(
                s,
                job_id=budget_job,
                action="retry",
                note=None,
                requested_by="x",
                amount_usd=Decimal("1"),
            )
