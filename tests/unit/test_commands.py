import pytest
from sqlalchemy import func, select, update
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.commands import (
    GATE_ACTIONS,
    CommandRefused,
    action_allowed,
    check_command_allowed,
    submit_command,
    to_resume,
)
from swfactory.db.repo import command_by_source_ref
from swfactory.db.tables import Command, Job
from swfactory.domain.types import TERMINAL_STAGES, HumanAction, Stage


def test_approve_resume_payload():
    c = to_resume(HumanAction.APPROVE, note=None, by="ann", retry_offered=False)
    assert c is not None
    assert c.resume == {
        "action": "approve",
        "note": "",
        "by": "ann",
        "command_id": "",
        "head_sha": None,
    }


def test_resume_carries_head():
    c = to_resume(HumanAction.APPROVE, note=None, by="ann", retry_offered=False, head_sha="a" * 40)
    assert c is not None and isinstance(c.resume, dict)
    assert c.resume["head_sha"] == "a" * 40


def test_cancel_is_not_a_resume():
    assert to_resume(HumanAction.CANCEL, note=None, by="ann", retry_offered=False) is None


def test_rework_needs_note():
    with pytest.raises(CommandRefused, match="rework needs a note"):
        to_resume(HumanAction.REWORK, note=" ", by="ann", retry_offered=False)


def test_retry_only_when_offered():
    with pytest.raises(CommandRefused, match="retry not offered"):
        to_resume(HumanAction.RETRY, note=None, by="ann", retry_offered=False)
    c = to_resume(HumanAction.RETRY, note=None, by="ann", retry_offered=True)
    assert c is not None
    assert isinstance(c.resume, dict)
    assert c.resume["action"] == "retry"


@pytest.mark.parametrize(
    "action,stage,ok",
    [(a, Stage.NEEDS_HUMAN, True) for a in GATE_ACTIONS]
    + [(a, Stage.BUILD, False) for a in GATE_ACTIONS]
    + [(HumanAction.CANCEL, s, s not in TERMINAL_STAGES) for s in Stage],
)
def test_which_stages_accept_which_action(action, stage, ok):
    if ok:
        assert check_command_allowed(action, stage, "n") is None
    else:
        with pytest.raises(CommandRefused):
            check_command_allowed(action, stage, "n")


def test_crash_backoff_and_default():
    from swfactory.definition.models import Defaults
    from swfactory.worker import crash_backoff_s

    assert [crash_backoff_s(n) for n in (1, 2, 3)] == [60, 120, 240]
    assert Defaults().max_job_crashes == 3


@pytest.mark.parametrize(
    "action,ok",
    [
        (HumanAction.RETRY, True),
        (HumanAction.CANCEL, True),
        (HumanAction.APPROVE, False),
        (HumanAction.REWORK, False),
        (HumanAction.REJECT, False),
    ],
)
def test_failed_job_accepts_retry_and_cancel(action, ok):
    if ok:
        assert check_command_allowed(action, Stage.FAILED, "n") is None
    else:
        with pytest.raises(CommandRefused):
            check_command_allowed(action, Stage.FAILED, "n")


def test_add_budget_resumes_like_retry():
    cmd = to_resume(HumanAction.ADD_BUDGET, note=None, by="ad", retry_offered=True)
    assert cmd is not None
    r: dict = cmd.resume  # pyright: ignore[reportAssignmentType]
    assert (r["action"], r["by"]) == ("add_budget", "ad")
    with pytest.raises(CommandRefused, match="retry not offered"):
        to_resume(HumanAction.ADD_BUDGET, note=None, by="ad", retry_offered=False)


def test_add_budget_allowed_where_retry_is():
    got = [
        action_allowed(HumanAction.ADD_BUDGET, stage=Stage.NEEDS_HUMAN, retry_offered=o)
        for o in (True, False)
    ]
    assert got == [True, False]
    assert action_allowed(HumanAction.ADD_BUDGET, stage=Stage.BUILD, retry_offered=True) is False


@pytest.fixture
async def waiting_job(sessions):
    """A needs_human job whose branch head is ``a * 40`` and whose wait event shows no head."""
    from swfactory.events import record_event

    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    async with sessions.begin() as s:
        await s.execute(
            update(Job).where(Job.id == job.id).values(stage="needs_human", head_sha="a" * 40)
        )
        await record_event(s, job.id, "human.waiting", {"reason": "reviewer failed"})
    return job.id


async def _count_commands(sessions, job_id) -> int:
    async with sessions() as s:
        return await s.scalar(
            select(func.count()).select_from(Command).where(Command.job_id == job_id)
        )


@pytest.mark.db
async def test_duplicate_source_ref_one_row(sessions, waiting_job):
    kw = dict(
        job_id=waiting_job,
        action=HumanAction.APPROVE,
        note=None,
        requested_by="github:ana",
        head_sha="a" * 40,
        source_ref="gh-comment:9",
    )
    async with sessions.begin() as s:
        first = await submit_command(s, **kw)  # type: ignore[arg-type]
    async with sessions.begin() as s:
        assert await submit_command(s, **kw) == first  # type: ignore[arg-type]
    async with sessions() as s:
        assert await command_by_source_ref(s, "gh-comment:9") == first
        assert await command_by_source_ref(s, "gh-comment:10") is None
    assert await _count_commands(sessions, waiting_job) == 1


@pytest.mark.db
async def test_unpinned_approve_stores_job_head(sessions, waiting_job):
    async with sessions.begin() as s:
        cid = await submit_command(
            s,
            job_id=waiting_job,
            action=HumanAction.APPROVE,
            note=None,
            requested_by="cli:ann",
        )
    async with sessions() as s:
        assert (await s.get_one(Command, cid)).head_sha == "a" * 40
