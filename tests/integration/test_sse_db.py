import asyncio
import json

import pytest
from sqlalchemy import func, select
from tests.support.pg import libpq

from swfactory.api.sse import event_stream
from swfactory.db.tables import App, Job, JobEvent
from swfactory.events import EventBus

pytestmark = pytest.mark.db


async def _job(sessions, slug, seq):
    async with sessions() as s, s.begin():
        app = (await s.execute(select(App).where(App.slug == slug))).scalar_one_or_none()
        if app is None:
            app = App(slug=slug, repo_kind="local", repo_url="/tmp/x")
            s.add(app)
            await s.flush()
        job = Job(
            app_id=app.id,
            seq=seq,
            slug=f"j{slug}{seq}",
            title="t",
            feature="f",
            requested_by="me",
            budget_usd=1,
        )
        s.add(job)
        await s.flush()
        return job.id


@pytest.fixture
async def api_db(sessions, pg_url):
    from types import SimpleNamespace

    return SimpleNamespace(bus=EventBus(sessions, libpq(pg_url), poll_interval_s=0.2))


@pytest.fixture
async def job_a(sessions):
    return await _job(sessions, "todo", 1)


@pytest.fixture
async def job_b(sessions):
    return await _job(sessions, "other", 1)


async def test_emit_reaches_subscriber_filtered_by_app(sessions, api_db, job_a, job_b):
    async with sessions() as s:
        start = int(await s.scalar(select(func.coalesce(func.max(JobEvent.id), 0))) or 0)
    stream = event_stream(api_db.bus, app_slug="todo", after_id=start, ping_s=60)
    await api_db.bus.emit(job_b, "job.stage", {"stage": "spec"})
    await api_db.bus.emit(job_a, "job.stage", {"stage": "build"})
    chunk = await asyncio.wait_for(anext(stream), 5)
    data = json.loads(chunk.split("data: ", 1)[1])
    assert data["job_id"] == str(job_a) and data["stage"] == "build"
    await stream.aclose()
