import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from tests.unit.api.api_support import PERSONAL_TOKEN, FakeKeys, fake_deps, settings

from swfactory.api.app import create_app
from swfactory.api.sse import event_stream, format_sse
from swfactory.events import Event

JOB = UUID("11111111-1111-1111-1111-111111111111")
AT = datetime(2026, 1, 1, tzinfo=UTC)
AUTH = {"authorization": f"Bearer {PERSONAL_TOKEN}"}


class FakeBus:
    def __init__(self, events=()):
        self.args = None
        self._events = list(events)

    async def subscribe(self, *, app_slug, after_id, app_slugs=None):
        self.args = {"app_slug": app_slug, "after_id": after_id, "app_slugs": app_slugs}
        for e in self._events:
            yield e


class SilentBus:
    async def subscribe(self, *, app_slug, after_id, app_slugs=None):
        await asyncio.sleep(3600)
        yield  # pragma: no cover


@pytest.fixture
def fake_bus():
    return FakeBus([Event(42, JOB, AT, "job.stage", {"stage": "build"})])


@pytest.fixture
def client(fake_bus):
    s = settings()
    d = fake_deps(s)
    object.__setattr__(d, "events", fake_bus)
    app = create_app(s, d, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]
    return TestClient(app, base_url="http://127.0.0.1:8080")


def test_format_sse():
    assert (
        format_sse(Event(42, JOB, AT, "job.stage", {"stage": "build"}))
        == f'id: 42\nevent: job.stage\ndata: {{"job_id": "{JOB}", "stage": "build"}}\n\n'
    )


def test_stream_response(client):
    r = client.get("/api/events", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"
    assert r.text.startswith("retry: 3000\n\n")
    assert "id: 42\nevent: job.stage\n" in r.text


def test_requires_auth(client):
    assert client.get("/api/events").status_code == 401


def test_resumes_from_last_event_id(fake_bus, client):
    client.get("/api/events?app=todo", headers={**AUTH, "Last-Event-ID": "41"})
    assert fake_bus.args == {"app_slug": "todo", "after_id": 41, "app_slugs": None}


def test_after_query_used_when_no_header(fake_bus, client):
    client.get("/api/events?after=7", headers=AUTH)
    assert fake_bus.args is not None and fake_bus.args["after_id"] == 7


def test_header_wins_over_query(fake_bus, client):
    client.get("/api/events?after=7", headers={**AUTH, "Last-Event-ID": "9"})
    assert fake_bus.args is not None and fake_bus.args["after_id"] == 9


@pytest.mark.parametrize("bad", ["abc", "-1"])
def test_bad_last_event_id_400(client, bad):
    r = client.get("/api/events", headers={**AUTH, "Last-Event-ID": bad})
    assert r.status_code == 400


async def test_ping_when_idle():
    stream = event_stream(SilentBus(), app_slug=None, after_id=None, ping_s=0.01)  # pyright: ignore[reportArgumentType]
    assert await anext(stream) == ": ping\n\n"
    await stream.aclose()


async def test_stream_yields_events():
    bus = FakeBus([Event(1, None, AT, "factory.halted", {})])
    chunks = [c async for c in event_stream(bus, app_slug=None, after_id=None)]  # pyright: ignore[reportArgumentType]
    assert chunks == ['id: 1\nevent: factory.halted\ndata: {"job_id": null}\n\n']
