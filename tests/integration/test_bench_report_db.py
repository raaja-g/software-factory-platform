from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from swfactory.api.routes.bench import load_run, load_runs, report_json
from swfactory.bench.report import build_report
from swfactory.db.tables import BenchResult, BenchRun

pytestmark = pytest.mark.db

TASKS = ("001-a", "002-b", "003-c", "004-d")


async def _clean(sessions) -> None:
    async with sessions.begin() as s:
        await s.execute(text("truncate app.bench_results, app.bench_runs cascade"))


@pytest.fixture
async def seeded(sessions):
    await _clean(sessions)
    try:
        async with sessions.begin() as s:
            run = BenchRun(
                suite="v1", configurations=[], status="done", budget_usd=Decimal("40"),
                cost_usd=Decimal("9"),
            )  # fmt: skip
            s.add(run)
            await s.flush()
            for cfg, cost in (("gemini", "1.20"), ("claude", "2.40")):
                for t in TASKS:
                    s.add(
                        BenchResult(
                            run_id=run.id, task=t, configuration=cfg, status="done",
                            hidden_passed=True, rounds=2, cost_usd=Decimal(cost),
                            duration_s=Decimal("30"),
                        )
                    )  # fmt: skip
        yield run.id
    finally:
        await _clean(sessions)


async def test_loaders_feed_the_report(sessions, seeded):
    async with sessions() as s:
        runs = await load_runs(s)
        found = await load_run(s, seeded)
        missing = await load_run(s, UUID(int=1))
    assert missing is None and found is not None
    assert [r["id"] for r, _ in runs] == [seeded] and len(runs[0][1]) == 8
    run, results = found
    body = report_json(build_report(run, results), results)
    assert body["recommended"] == "gemini" and body["id"] == str(seeded)
    assert body["cost_usd"] == "9.000000"
