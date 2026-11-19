import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text, update
from tests.support.db_helpers import activate, enq
from tests.unit.api.api_support import FakeKeys, server, settings

from swfactory.api.app import ApiDeps, create_app
from swfactory.artifacts import ArtifactStore
from swfactory.db.tables import Job
from swfactory.definition.models import Defaults
from swfactory.definition.store import DefinitionStore
from swfactory.events import EventBus
from swfactory.health import beat, detect_stuck, live_workers

pytestmark = pytest.mark.db

NOW = datetime.now(UTC)


@pytest.fixture
async def s(sessions):
    async with sessions() as x:
        yield x


@pytest.fixture
async def s1(sessions):
    async with sessions() as x:
        yield x


@pytest.fixture
async def s2(sessions):
    async with sessions() as x:
        yield x


@pytest.fixture
def bus(sessions):
    return EventBus(sessions, "")


async def _job_quiet(sessions, stage):
    await activate(sessions, apps={"demo": {}})
    job = await enq(sessions, app="demo")
    async with sessions.begin() as s:
        await s.execute(update(Job).where(Job.id == job.id).values(stage=stage))
        await s.execute(text("delete from app.job_events where job_id = :j"), {"j": job.id})
        await s.execute(
            text(
                "insert into app.job_events (job_id, at, kind, data) "
                "values (:j, :at, 'run.started', '{}')"
            ),
            {"j": job.id, "at": NOW - timedelta(minutes=40)},
        )
    return job.id


@pytest.fixture
async def build_job_quiet_40m(sessions):
    return await _job_quiet(sessions, "build")


@pytest.fixture
async def needs_human_job_quiet_40m(sessions, build_job_quiet_40m):
    async with sessions.begin() as s:
        n = await s.scalar(text("select count(*) from app.jobs"))
    assert n == 1
    job = await enq(sessions, app="demo", feature="Another feature")
    async with sessions.begin() as s:
        await s.execute(update(Job).where(Job.id == job.id).values(stage="needs_human"))
        await s.execute(text("delete from app.job_events where job_id = :j"), {"j": job.id})
        await s.execute(
            text(
                "insert into app.job_events (job_id, at, kind, data) "
                "values (:j, :at, 'run.started', '{}')"
            ),
            {"j": job.id, "at": NOW - timedelta(minutes=40)},
        )
    return job.id


async def count_events(s, job_id, kind):
    return await s.scalar(
        text("select count(*) from app.job_events where job_id = :j and kind = :k"),
        {"j": job_id, "k": kind},
    )


async def test_beat_upserts(s):
    await beat(s, worker_id="w1", jobs=1, version="v")
    await beat(s, worker_id="w1", jobs=2, version="v")
    rows = (await s.execute(text("select worker_id, jobs from app.worker_heartbeats"))).all()
    assert [tuple(r) for r in rows] == [("w1", 2)]
    assert await live_workers(s, within_s=2 * Defaults().lease_renew_s) == 1


async def test_stale_heartbeat_is_not_live(s):
    await beat(s, worker_id="w1", jobs=0, version="v")
    await s.execute(text("update app.worker_heartbeats set at = now() - interval '10 minutes'"))
    await s.commit()
    assert await live_workers(s, within_s=60) == 0


async def test_detector_emits_once_for_active_job_only(
    s, bus, build_job_quiet_40m, needs_human_job_quiet_40m
):
    assert await detect_stuck(s, bus, now=NOW) == [build_job_quiet_40m]
    assert await detect_stuck(s, bus, now=NOW + timedelta(minutes=45)) == []
    assert await count_events(s, build_job_quiet_40m, "job.stuck") == 1
    assert await count_events(s, needs_human_job_quiet_40m, "job.stuck") == 0


async def test_detector_single_holder(s1, s2, bus, build_job_quiet_40m):
    a, b = await asyncio.gather(detect_stuck(s1, bus, now=NOW), detect_stuck(s2, bus, now=NOW))
    assert sorted([len(a), len(b)]) == [0, 1]


async def test_new_event_starts_new_stall(s, bus, build_job_quiet_40m):
    await detect_stuck(s, bus, now=NOW)
    await bus.emit(build_job_quiet_40m, "run.started", {})
    # the event is stamped by the DB clock, so measure from the real present, not import time
    assert await detect_stuck(s, bus, now=datetime.now(UTC) + timedelta(minutes=31)) == [
        build_job_quiet_40m
    ]


async def _api(sessions, tmp_path, st):
    await activate(sessions, apps={"demo": {}})
    deps = ApiDeps(
        sessions=sessions,
        events=EventBus(sessions, ""),
        artifacts=ArtifactStore(sessions, tmp_path),
        catalog=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        definitions=DefinitionStore(sessions),
        users=SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        writer=None,
        settings=st,
    )
    app = create_app(st, deps, on_gce=lambda: False, iap_keys=FakeKeys())  # pyright: ignore[reportArgumentType]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    )


async def test_readyz_503_in_server_mode_without_live_worker(s, sessions, tmp_path):
    async with await _api(sessions, tmp_path, server()) as server_api:
        r = await server_api.get("/readyz")
        assert (r.status_code, r.json()["workers"]) == (503, 0)
        await beat(s, worker_id="w1", jobs=0, version="v")
        assert (await server_api.get("/readyz")).status_code == 200


async def test_readyz_personal_mode_ignores_workers(sessions, tmp_path):
    async with await _api(sessions, tmp_path, settings()) as personal_api:
        assert (await personal_api.get("/readyz")).status_code == 200
