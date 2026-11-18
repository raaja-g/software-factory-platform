import asyncio
from unittest.mock import AsyncMock

import psycopg
import pytest
from tests.support.pg import libpq

from swfactory.db.tables import App, Job
from swfactory.events import EventBus, record_event, stream_position

pytestmark = pytest.mark.db


def bus(sessions, pg_url, **kw):
    return EventBus(sessions, libpq(pg_url), **kw)


async def seed_job(sessions, app_slug="demo"):
    from sqlalchemy import select

    async with sessions() as s, s.begin():
        app = (await s.execute(select(App).where(App.slug == app_slug))).scalar_one_or_none()
        if app is None:
            app = App(slug=app_slug, repo_kind="local", repo_url="/tmp/x")
            s.add(app)
            await s.flush()
        seq = len((await s.execute(select(Job.id))).all()) + 1
        job = Job(
            app_id=app.id,
            seq=seq,
            slug=f"j{seq}",
            title="t",
            feature="f",
            requested_by="me",
            budget_usd=1,
        )
        s.add(job)
        await s.flush()
        return job.id


async def test_subscriber_gets_new_event(sessions, pg_url):
    b = bus(sessions, pg_url)
    job = await seed_job(sessions)
    it = b.subscribe()
    nxt = asyncio.ensure_future(anext(it))
    await asyncio.sleep(0.2)
    await b.emit(job, "job.stage", {"stage": "spec"})
    ev = await asyncio.wait_for(nxt, 2)
    assert (ev.kind, ev.job_id, ev.data) == ("job.stage", job, {"stage": "spec"})
    await it.aclose()


async def test_after_id_replays_in_order(sessions, pg_url):
    b = bus(sessions, pg_url)
    job = await seed_job(sessions)
    async with sessions() as s, s.begin():
        ids = [
            await record_event(s, job, "job.stage", {"stage": st})
            for st in ("triage", "spec", "build")
        ]
    it = b.subscribe(after_id=ids[0])
    got = [await asyncio.wait_for(anext(it), 2) for _ in range(2)]
    assert [(e.id, e.data["stage"]) for e in got] == [(ids[1], "spec"), (ids[2], "build")]
    await it.aclose()


async def test_app_filter(sessions, pg_url):
    b = bus(sessions, pg_url)
    ja, jb = await seed_job(sessions, "a"), await seed_job(sessions, "b")
    async with sessions() as s, s.begin():
        first = await record_event(s, jb, "job.stage", {"stage": "spec"})
        await record_event(s, None, "definition.loaded", {})
        last = await record_event(s, ja, "job.stage", {"stage": "spec"})
    it = b.subscribe(app_slug="a", after_id=first - 1)
    assert (await asyncio.wait_for(anext(it), 2)).id == last
    await it.aclose()


async def test_app_slugs_includes_jobless(sessions, pg_url):
    b = bus(sessions, pg_url)
    ja, jb = await seed_job(sessions, "a"), await seed_job(sessions, "b")
    async with sessions() as s, s.begin():
        first = await record_event(s, jb, "job.stage", {"stage": "spec"})
        gl = await record_event(s, None, "definition.loaded", {})
        last = await record_event(s, ja, "job.stage", {"stage": "spec"})
    it = b.subscribe(app_slugs=["a"], after_id=first - 1)
    got = [await asyncio.wait_for(anext(it), 2) for _ in range(2)]
    assert [e.id for e in got] == [gl, last]
    await it.aclose()


async def test_polling_fallback(monkeypatch, sessions, pg_url):
    monkeypatch.setattr(
        psycopg.AsyncConnection, "connect", AsyncMock(side_effect=OSError("no listen"))
    )
    b = bus(sessions, pg_url, poll_interval_s=0.2)
    job = await seed_job(sessions)
    it = b.subscribe()
    nxt = asyncio.ensure_future(anext(it))
    await asyncio.sleep(0.1)
    await b.emit(job, "job.stage", {"stage": "build"})
    assert (await asyncio.wait_for(nxt, 1)).data == {"stage": "build"}
    await it.aclose()


async def test_event_committed_after_a_later_id_is_not_skipped(sessions, pg_url):
    b = bus(sessions, pg_url, poll_interval_s=0.1)
    job = await seed_job(sessions)
    it = b.subscribe()
    nxt = asyncio.ensure_future(anext(it))
    await asyncio.sleep(0.2)
    async with sessions() as slow:
        early = await record_event(slow, job, "human.waiting", {"reason": "x"})
        await b.emit(job, "cost.updated", {"usd": "1"})
        await asyncio.sleep(0.3)
        await slow.commit()
    got = [await asyncio.wait_for(nxt, 2), await asyncio.wait_for(anext(it), 2)]
    assert [(e.id == early, e.kind) for e in got] == [
        (True, "human.waiting"),
        (False, "cost.updated"),
    ]
    await it.aclose()


async def test_stream_position_stops_before_open_transactions(sessions):
    job = await seed_job(sessions)
    async with sessions() as slow, sessions() as s:
        done = await record_event(s, job, "job.stage", {"stage": "spec"})
        await s.commit()
        await record_event(slow, job, "job.stage", {"stage": "build"})
        assert await stream_position(s) == done


async def test_subscribers_share_one_listen_connection(monkeypatch, sessions, pg_url):
    real, opened = psycopg.AsyncConnection.connect, []

    async def counting(*a, **kw):
        if kw.get("autocommit"):
            opened.append(a)
        return await real(*a, **kw)

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", counting)
    b = bus(sessions, pg_url)
    job = await seed_job(sessions)
    its = [b.subscribe() for _ in range(3)]
    nxt = [asyncio.ensure_future(anext(it)) for it in its]
    await asyncio.sleep(0.2)
    await b.emit(job, "job.stage", {"stage": "spec"})
    assert [(await asyncio.wait_for(n, 2)).data for n in nxt] == [{"stage": "spec"}] * 3
    assert len(opened) == 1
    for it in its:
        await it.aclose()
