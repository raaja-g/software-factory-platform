from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.api.routes.apps import COLUMNS, app_stats
from swfactory.api.routes.definition import definition_status
from swfactory.db.tables import AgentRun
from swfactory.definition.loader import LoadResult
from swfactory.definition.store import DefinitionStore

pytestmark = pytest.mark.db

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


async def test_app_stats_counts_columns_and_todays_spend(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    await enq(sessions, app="demo")
    async with sessions() as s, s.begin():
        for attempt, (started, cost) in enumerate(
            ((NOW, "0.5"), (NOW.replace(day=31, year=2025), "9")), start=1
        ):
            s.add(
                AgentRun(
                    job_id=job.id,
                    role="builder",
                    engine="fake",
                    attempt=attempt,
                    started_at=started,
                    cost_usd=Decimal(cost),
                )
            )
    async with sessions() as s:
        stats = await app_stats(s, now=NOW)
    counts, cost = stats["demo"]
    assert counts == dict.fromkeys(COLUMNS, 0) | {"queued": 2}
    assert cost == Decimal("0.5")


async def test_definition_status_reports_active_and_last_rejected(sessions):
    store = DefinitionStore(sessions)
    sha = await seed_demo(sessions)
    deps = SimpleNamespace(sessions=sessions)
    st = await definition_status(deps)  # pyright: ignore[reportArgumentType]
    assert st["active_sha"] == sha and st["last_rejected"] is None and st["loaded_at"]
    from swfactory.definition.loader import DefinitionError

    err = DefinitionError("factory.yaml", 3, "", "bad key")
    assert not await store.save(LoadResult("bad-sha", "src", None, (err,)))
    st = await definition_status(deps)  # pyright: ignore[reportArgumentType]
    assert st["active_sha"] == sha
    assert st["last_rejected"]["sha"] == "bad-sha" and "bad key" in st["last_rejected"]["errors"][0]
