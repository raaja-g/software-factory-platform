from datetime import UTC, datetime, timedelta

from tests.unit.api.api_support import settings
from tests.unit.api.test_auth import make_client

from swfactory.events import EVENT_KINDS
from swfactory.graph import steps
from swfactory.health import STUCK_AFTER, is_stuck

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def test_stuck_after_30_min():
    assert (
        is_stuck(last_event_at=NOW - timedelta(minutes=31), last_event_kind="run.started", now=NOW)
        is True
    )


def test_not_stuck_under_30():
    assert (
        is_stuck(last_event_at=NOW - timedelta(minutes=29), last_event_kind="run.started", now=NOW)
        is False
    )


def test_once_per_stall():
    assert (
        is_stuck(last_event_at=NOW - timedelta(hours=2), last_event_kind="job.stuck", now=NOW)
        is False
    )


def test_stuck_after_value():
    assert STUCK_AFTER == timedelta(minutes=30) and "job.stuck" in EVENT_KINDS


def test_progress_interval():
    assert steps.PROGRESS_EVERY_S == 300 and "run.progress" in EVENT_KINDS


def test_readyz_reports_disk(monkeypatch):
    client_personal = make_client(settings())
    monkeypatch.setattr("swfactory.api.app.disk_usage_pct", lambda paths: (91, "/"))
    r = client_personal.get("/readyz")
    assert r.status_code == 200 and r.json()["disk"] == {"pct": 91, "path": "/", "high": True}
