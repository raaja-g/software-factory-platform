from decimal import Decimal

import pytest

from swfactory.api.routes.board import V1_KEYS, board_job, column_for
from swfactory.api.routes.jobs import BudgetRefused, effective_budget
from swfactory.commands import action_allowed, retry_offered
from swfactory.domain.types import TERMINAL_STAGES, HumanAction, Stage

D10, D25 = Decimal("10"), Decimal("25")

ROW = {
    "job_id": "7c1d0b0e-0000-4000-8000-000000000001",
    "seq": 3,
    "slug": "todo-web",
    "app": "todo",
    "title": "Todo web",
    "stage": "verify",
    "round": 1,
    "updated": "2026-01-01T00:00:00Z",
}


def test_column_for_covers_every_stage():
    assert {column_for(s) for s in Stage} == {
        "queued", "triage", "spec", "build", "review", "approve", "needs_human", "done",
    }  # fmt: skip


def test_verify_is_build_column():
    assert column_for(Stage.VERIFY) == "build"


def test_actions_only_in_needs_human():
    assert action_allowed(HumanAction.APPROVE, stage=Stage.FAILED, retry_offered=True) is False
    assert action_allowed(HumanAction.REWORK, stage=Stage.BUILD, retry_offered=False) is False
    assert action_allowed(HumanAction.APPROVE, stage=Stage.NEEDS_HUMAN, retry_offered=False)


def test_retry_follows_graph_flag():
    assert action_allowed(HumanAction.RETRY, stage=Stage.NEEDS_HUMAN, retry_offered=True) is True
    assert action_allowed(HumanAction.RETRY, stage=Stage.NEEDS_HUMAN, retry_offered=False) is False


def test_failed_and_delivered_jobs_have_exits():
    f = Stage.FAILED
    assert [
        action_allowed(a, stage=f, retry_offered=False)
        for a in (HumanAction.RETRY, HumanAction.CANCEL, HumanAction.REWORK)
    ] == [True, True, False]
    d = Stage.DELIVERED
    assert [
        action_allowed(a, stage=d, retry_offered=False)
        for a in (HumanAction.CANCEL, HumanAction.RETRY, HumanAction.APPROVE)
    ] == [True, False, False]
    assert [
        (a, st)
        for a in HumanAction
        for st in TERMINAL_STAGES
        if action_allowed(a, stage=st, retry_offered=True)
    ] == []


def test_cancel_in_queued_and_active_stages():
    for st in (Stage.QUEUED, Stage.TRIAGE, Stage.BUILD, Stage.DELIVERING, Stage.NEEDS_HUMAN):
        assert action_allowed(HumanAction.CANCEL, stage=st, retry_offered=False)


def test_retry_offered_from_waiting_event():
    assert [
        retry_offered({"reason": "checks failed", "retry_node": "verify", "artifacts": []}),
        retry_offered({"reason": "approver escalated", "retry_node": None, "artifacts": ["d.md"]}),
        retry_offered(None),
    ] == [True, False, False]


def test_budget_clamp():
    assert effective_budget(None, default=D10, job_max=D25, is_admin=False) == D10
    with pytest.raises(BudgetRefused):
        effective_budget(Decimal("11"), default=D10, job_max=D25, is_admin=False)
    assert effective_budget(Decimal("20"), default=D10, job_max=D25, is_admin=True) == Decimal("20")
    with pytest.raises(BudgetRefused):
        effective_budget(Decimal("26"), default=D10, job_max=D25, is_admin=True)


def test_board_job_keeps_v1_keys():
    job = board_job(ROW)
    assert V1_KEYS <= job.keys()
    assert (job["id"], job["ref"], job["column"]) == ("003-todo-web", "todo:003", "build")
    assert job["branch"] == "factory/003-todo-web" and job["quality"] is None
    assert job["retry_offered"] is False and job["stuck"] is False
    assert (job["kind"], job["backlog_ref"], job["backlog_pos"]) == ("normal", None, None)


def test_failed_job_offers_retry_while_graph_supported():
    assert board_job({**ROW, "stage": "failed"})["retry_offered"] is True
    assert board_job({**ROW, "stage": "failed", "graph_supported": False})["retry_offered"] is False


def test_waiting_head_and_retry_flag():
    w = {"reason": "checks failed", "retry_node": "verify", "head_sha": "a" * 40}
    job = board_job({**ROW, "stage": "needs_human", "waiting": w})
    assert (job["column"], job["retry_offered"], job["head_sha"]) == ("needs_human", True, "a" * 40)
