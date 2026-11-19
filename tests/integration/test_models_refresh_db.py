from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from tests.fakes.gateway import FakeGateway

from swfactory.db.tables import JobEvent
from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.definition.store import DefinitionStore
from swfactory.events import record_event
from swfactory.models.catalog import Catalog, catalog_tick

pytestmark = pytest.mark.db

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
FLASH = "gemini-flash@vertex"


class RecordingCatalog(Catalog):
    refresh_calls: list

    async def refresh(self, aliases=None):
        self.refresh_calls.append(None if aliases is None else list(aliases))
        return await super().refresh(aliases)


@pytest.fixture
async def catalog_fake_gateway(sessions):
    r = load_definition(str(defaults_dir()))
    assert r.definition is not None
    store = DefinitionStore(sessions)
    assert await store.save(r)
    gw = FakeGateway()
    gw.prices = {a: (Decimal(1), Decimal(2), 100_000) for a in r.definition.factory.models}
    cat = RecordingCatalog(sessions, gw, store)  # pyright: ignore[reportArgumentType]
    cat.refresh_calls = []
    cat.fake = gw  # pyright: ignore[reportAttributeAccessIssue]
    # everything probed at NOW, so the due check has nothing to do
    await Catalog.refresh(cat)
    cat.refresh_calls.clear()
    async with sessions() as s, s.begin():
        from sqlalchemy import text

        await s.execute(text("update app.models set last_probe_at = :t"), {"t": NOW})
    return cat


async def last_event_data(sessions, kind):
    async with sessions() as s:
        return await s.scalar(
            select(JobEvent.data).where(JobEvent.kind == kind).order_by(JobEvent.id.desc()).limit(1)
        )


async def request(sessions, aliases):
    async with sessions() as s, s.begin():
        return await record_event(
            s, None, "models.refresh_requested", {"aliases": aliases, "by": "a@x.io"}
        )


async def test_catalog_tick_serves_a_request_once(sessions, catalog_fake_gateway):
    rid = await request(sessions, [FLASH])
    assert await catalog_tick(sessions, catalog_fake_gateway, now=NOW) == rid
    assert await catalog_tick(sessions, catalog_fake_gateway, now=NOW) is None
    assert catalog_fake_gateway.refresh_calls == [[FLASH]]  # pyright: ignore[reportAttributeAccessIssue]
    assert await last_event_data(sessions, "models.refreshed") == {
        "request_id": rid,
        "aliases": [FLASH],
        "available": 1,
    }


async def test_null_aliases_refresh_everything_oldest_first(sessions, catalog_fake_gateway):
    first = await request(sessions, None)
    second = await request(sessions, [FLASH])
    assert await catalog_tick(sessions, catalog_fake_gateway, now=NOW) == first
    assert await catalog_tick(sessions, catalog_fake_gateway, now=NOW) == second
    assert catalog_fake_gateway.refresh_calls == [None, [FLASH]]  # pyright: ignore[reportAttributeAccessIssue]


async def test_unreachable_gateway_leaves_request_open(sessions, catalog_fake_gateway):
    rid = await request(sessions, [FLASH])
    gw = catalog_fake_gateway.fake
    gw.probe_error = httpx.ConnectError("down")
    assert await catalog_tick(sessions, catalog_fake_gateway, now=NOW) is None
    assert await last_event_data(sessions, "models.refreshed") is None
    gw.probe_error = None
    assert await catalog_tick(sessions, catalog_fake_gateway, now=NOW) == rid


async def test_no_request_runs_the_due_check(sessions, catalog_fake_gateway):
    assert await catalog_tick(sessions, catalog_fake_gateway, now=NOW) is None
    assert catalog_fake_gateway.refresh_calls == []  # pyright: ignore[reportAttributeAccessIssue]
    later = NOW.replace(day=3)  # more than 24 h on: a full nightly refresh
    assert await catalog_tick(sessions, catalog_fake_gateway, now=later) is None
    assert len(catalog_fake_gateway.refresh_calls) == 1  # pyright: ignore[reportAttributeAccessIssue]
