from datetime import UTC, datetime, timedelta

from swfactory.definition.models import Defaults
from swfactory.events import EVENT_KINDS
from swfactory.expiry import expiry_warning_due, waiting_expired

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def test_expired_after_the_days():
    assert waiting_expired(NOW - timedelta(days=14, seconds=1), NOW, 14)
    assert not waiting_expired(NOW - timedelta(days=13, hours=23), NOW, 14)


def test_zero_disables():
    assert not waiting_expired(NOW - timedelta(days=400), NOW, 0)


def test_warning_two_days_before():
    assert expiry_warning_due(NOW - timedelta(days=12, hours=1), NOW, 14)
    assert not expiry_warning_due(NOW - timedelta(days=11), NOW, 14)
    assert not expiry_warning_due(NOW - timedelta(days=15), NOW, 14)
    assert not expiry_warning_due(NOW - timedelta(days=12, hours=1), NOW, 0)


def test_default_and_event():
    assert Defaults().waiting_expiry_days == 14 and "job.expired" in EVENT_KINDS
