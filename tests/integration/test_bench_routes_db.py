"""M10.13: the bench routes against the real estimate and start code and a real database."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from tests.fakes.deps import seed_demo
from tests.unit.api.api_support import FakeKeys, server
from tests.unit.api.test_actions_auth import TOKENS, Artifacts, Events, Users

from swfactory.api.app import ApiDeps, create_app
from swfactory.cli.api_client import ApiClient, ApiError
from swfactory.db.tables import BenchResult, BenchRun
from swfactory.definition.models import BenchConfigDef, BenchSuiteDef, BenchTaskDef
from swfactory.definition.store import DefinitionStore

pytestmark = pytest.mark.db

SHA = "a" * 40
TASK = BenchTaskDef(
    title="t", feature="f", repo="local:demo", base_sha=SHA, merge_sha=SHA, definition_sha=SHA,
    check_command="pytest {file}", scorers=("merge-readiness",),
)  # fmt: skip
SUITE = BenchSuiteDef(
    configurations=(BenchConfigDef(name="c1", profile="team-a"),),
    tasks={"001": TASK, "002": TASK},
)
TABLES = "app.bench_results, app.bench_runs"


async def _clean(sessions):
    async with sessions.begin() as s:
        await s.execute(text(f"truncate {TABLES} cascade"))


@pytest.fixture
async def api(sessions):
    await _clean(sessions)
    await seed_demo(sessions, benchmarks={"v1-parity": SUITE})
    settings = server()
    deps = ApiDeps(
        sessions=sessions, events=Events(), artifacts=Artifacts(),  # pyright: ignore[reportArgumentType]
        catalog=SimpleNamespace(), definitions=DefinitionStore(sessions),  # pyright: ignore[reportArgumentType]
        users=Users(), writer=None, settings=settings,  # pyright: ignore[reportArgumentType]
    )  # fmt: skip
    app = create_app(settings, deps, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]
    client = TestClient(app, base_url="http://127.0.0.1:8080")
    client.headers["authorization"] = f"Bearer {TOKENS['admin']}"
    c = ApiClient("http://127.0.0.1:8080", TOKENS["admin"])
    c._http = client  # pyright: ignore[reportAttributeAccessIssue]
    yield SimpleNamespace(client=client, cli=c, sessions=sessions)
    await _clean(sessions)


async def test_estimate_start_conflict_and_cancel(api):
    est = api.cli.bench_estimate("v1-parity", repetitions=2)
    assert est["trials"] == 4 and est["configs"] == ["c1"]
    with pytest.raises(ApiError) as e:
        api.cli.bench_run("v1-parity", accept_estimate_usd="0.01", repetitions=2)
    assert e.value.status == 409
    out = api.cli.bench_run("v1-parity", accept_estimate_usd=est["estimate_usd"], repetitions=2)
    async with api.sessions() as s:
        run = (await s.scalars(select(BenchRun))).one()
        n = len((await s.scalars(select(BenchResult))).all())
    assert str(run.id) == out["run_id"] and run.status == "running" and n == 4
    assert api.client.post(f"/api/bench/runs/{run.id}/cancel").status_code == 200
    assert api.client.post(f"/api/bench/runs/{run.id}/cancel").status_code == 409
    assert api.client.post(f"/api/bench/runs/{run.id.int + 1}/cancel").status_code == 404
    async with api.sessions() as s:
        assert (await s.scalars(select(BenchRun.status))).one() == "cancelled"


async def test_unknown_suite_is_422(api):
    with pytest.raises(ApiError) as e:
        api.cli.bench_estimate("nope")
    assert e.value.status == 422
