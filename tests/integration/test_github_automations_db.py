import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

import swfactory.automations.scheduler as scheduler
from swfactory.automations.scheduler import Scheduler, automation_block_reason
from swfactory.db.tables import AutomationFire
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import AppDef, AutomationDef
from swfactory.definition.store import DefinitionStore, NoActiveDefinition
from swfactory.queue import enqueue_job

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 11, 9, 1, tzinfo=UTC)


def auto(cron="0 9 * * *", **kw) -> AutomationDef:
    return AutomationDef.model_validate(
        {
            "trigger": {"provider": "schedule", "event": "cron_fired", "filter": {"cron": cron}},
            "app": "a",
            "feature_template": "Nightly chore",
            **kw,
        }
    )


async def activate(sessions, autos: dict[str, AutomationDef]):
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    base = next(iter(r.definition.factory.apps.values()))
    fac = r.definition.factory.model_copy(
        update={"apps": {"a": AppDef.model_validate(base.model_dump(mode="json"))}}
    )
    defn = r.definition.model_copy(update={"factory": fac, "automations": autos})
    r = replace(r, sha=f"sha-{datetime.now(UTC).timestamp()}", definition=defn)
    assert await DefinitionStore(sessions).save(r)
    return Scheduler(sessions, DefinitionStore(sessions))


async def add_fire(sessions, name, at, job_id=None):
    async with sessions.begin() as s:
        s.add(AutomationFire(automation=name, fire_at=at, job_id=job_id))


async def fires(sessions, name):
    async with sessions() as s:
        rows = await s.execute(
            select(AutomationFire.fire_at, AutomationFire.job_id)
            .where(AutomationFire.automation == name)
            .order_by(AutomationFire.fire_at)
        )
        return [tuple(r) for r in rows]


async def make_job(sessions, requested_by, *, stage="queued", created=None):
    async with sessions.begin() as s:
        j = await enqueue_job(
            s,
            app_slug="a",
            feature="x",
            title=None,
            source="schedule",
            source_ref=None,
            requested_by=requested_by,
        )
        await s.execute(
            text("UPDATE app.jobs SET stage = :st, created_at = :c WHERE id = :i"),
            {"st": stage, "c": created or datetime.now(UTC), "i": j.id},
        )
        return j.id


YESTERDAY = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
TODAY = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


async def test_one_job_per_fire_across_two_ticks(sessions):
    sched = await activate(sessions, {"nightly": auto()})
    await add_fire(sessions, "nightly", YESTERDAY)
    assert len(await sched.tick(NOW)) == 1
    assert await sched.tick(NOW) == []
    rows = await fires(sessions, "nightly")
    assert [r[0] for r in rows] == [YESTERDAY, TODAY] and rows[1][1] is not None


async def test_failed_enqueue_leaves_no_fire(sessions, monkeypatch):
    sched = await activate(sessions, {"nightly": auto()})
    await add_fire(sessions, "nightly", YESTERDAY)
    monkeypatch.setattr(scheduler, "enqueue_job", AsyncMock(side_effect=NoActiveDefinition()))
    assert await sched.tick(NOW) == [] and len(await fires(sessions, "nightly")) == 1
    monkeypatch.undo()
    assert len(await sched.tick(NOW)) == 1 and len(await fires(sessions, "nightly")) == 2


async def test_new_schedule_waits_for_its_next_slot(sessions):
    sched = await activate(sessions, {"nightly": auto()})
    assert await sched.tick(datetime(2026, 11, 11, 9, 7, tzinfo=UTC)) == []
    assert await fires(sessions, "nightly") == [(TODAY, None)]
    assert len(await sched.tick(datetime(2026, 11, 12, 9, 1, tzinfo=UTC))) == 1


async def test_open_job_cap_skips_the_fire(sessions):
    sched = await activate(sessions, {"hourly": auto("0 * * * *")})
    eight = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
    job8 = await make_job(sessions, "schedule:hourly")
    await add_fire(sessions, "hourly", eight, job8)
    assert await sched.tick(NOW) == []
    assert await fires(sessions, "hourly") == [(eight, job8), (TODAY, None)]


async def test_block_reason(sessions):
    await activate(sessions, {})
    await make_job(sessions, "schedule:hourly", created=NOW - timedelta(minutes=10))
    async with sessions.begin() as s:

        async def reason(**kw):
            return await automation_block_reason(
                s, name="hourly", auto=auto(**kw), requested_by="schedule:hourly", now=NOW
            )

        assert await reason(max_open_jobs=1) == "max_open_jobs"
        assert await reason(max_open_jobs=2, min_interval_s=3600) == "min_interval_s"
        assert await reason(max_open_jobs=2) is None


async def test_block_reason_counts_every_hook_and_finished_jobs(sessions):
    await activate(sessions, {})
    await make_job(sessions, "webhook:stripe/nightly", created=NOW - timedelta(hours=2))
    await make_job(
        sessions, "webhook:gh/nightly", stage="merged", created=NOW - timedelta(minutes=10)
    )
    await make_job(sessions, "webhook:gh/nightly_x", created=NOW - timedelta(minutes=1))
    async with sessions.begin() as s:

        async def reason(**kw):
            return await automation_block_reason(
                s, name="nightly", auto=auto(**kw), requested_by="webhook:gh/nightly", now=NOW
            )

        assert await reason(max_open_jobs=1) == "max_open_jobs"
        assert await reason(max_open_jobs=2, min_interval_s=3600) == "min_interval_s"
        assert await reason(max_open_jobs=2) is None
