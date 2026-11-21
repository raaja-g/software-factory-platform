from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from swfactory.definition.models import ImproverConfig
from swfactory.domain.types import Role
from swfactory.improve.signals import FailureSignal, group_by_role, trigger_due

NOW = datetime(2026, 11, 11, tzinfo=UTC)
CFG = ImproverConfig(enabled=True, bench_suite="v1-parity")


def sig(role: Role) -> FailureSignal:
    return FailureSignal(uuid4(), "j1", role, "failed", "x", NOW)


def test_threshold():
    d = trigger_due(CFG, failed_jobs=25, oldest_at=NOW - timedelta(days=1), now=NOW)
    assert d.reason == "threshold" and d.due


def test_below_threshold_and_fresh():
    d = trigger_due(CFG, failed_jobs=24, oldest_at=NOW - timedelta(days=6, hours=23), now=NOW)
    assert not d.due and d.reason is None


def test_age():
    assert (
        trigger_due(CFG, failed_jobs=1, oldest_at=NOW - timedelta(days=7), now=NOW).reason == "age"
    )


def test_threshold_wins_over_age():
    d = trigger_due(CFG, failed_jobs=30, oldest_at=NOW - timedelta(days=30), now=NOW)
    assert d.reason == "threshold"


def test_nothing_to_address():
    assert not trigger_due(CFG, failed_jobs=0, oldest_at=None, now=NOW).due


def test_disabled():
    d = trigger_due(ImproverConfig(), failed_jobs=99, oldest_at=NOW - timedelta(days=30), now=NOW)
    assert not d.due


def test_grouping_order():
    got = group_by_role([sig(Role.BUILDER), sig(Role.REVIEWER_SECURITY), sig(Role.BUILDER)])
    assert [r for r, _ in got] == [Role.BUILDER, Role.REVIEWER_SECURITY]
    tie = group_by_role([sig(Role.BUILDER), sig(Role.APPROVER)])
    assert [r for r, _ in tie] == [Role.APPROVER, Role.BUILDER]
