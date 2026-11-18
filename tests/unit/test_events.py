import pytest

from swfactory.events import EVENT_KINDS, Event, EventBus


async def test_emit_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown event kind 'job.exploded'"):
        await EventBus(sessions=None, dsn="").emit(None, "job.exploded", {})


def test_event_kinds_cover_core_and_exclude_nothing_shared():
    assert {"job.created", "job.stage", "backlog.created", "factory.halted"} <= EVENT_KINDS
    assert len(EVENT_KINDS) == 35


def test_event_is_frozen():
    ev = Event(1, None, None, "job.stage", {})  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        ev.id = 2  # type: ignore[misc]
