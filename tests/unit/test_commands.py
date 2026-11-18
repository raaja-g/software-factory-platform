import pytest

from swfactory.commands import GATE_ACTIONS, CommandRefused, check_command_allowed, to_resume
from swfactory.domain.types import TERMINAL_STAGES, HumanAction, Stage


def test_approve_resume_payload():
    c = to_resume(HumanAction.APPROVE, note=None, by="ann", retry_offered=False)
    assert c is not None
    assert c.resume == {"action": "approve", "note": "", "by": "ann", "command_id": ""}


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
