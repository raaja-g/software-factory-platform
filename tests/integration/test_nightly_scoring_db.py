from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.db.engine import make_engine
from swfactory.db.migrate import alembic_config
from swfactory.db.repo import load_job_facts, replace_scores
from swfactory.db.tables import Job, JobEvent, Score
from swfactory.events import record_event
from swfactory.scoring.objective import (
    OBJ_SCORERS,
    CommitMessage,
    compute_signals,
    nightly_scoring,
)

pytestmark = pytest.mark.db

NIGHT = datetime.now(UTC).replace(hour=3, minute=0, second=0, microsecond=0)
LATER = datetime.now(UTC) + timedelta(minutes=5)
MERGE = "ab12cd3" + "0" * 33
HEAD = "e" * 40


_CLEAN = text(
    "truncate app.job_events, app.scores, app.jobs, app.apps, app.maintenance_runs "
    "restart identity cascade"
)


@pytest.fixture(autouse=True)
async def _clean_after(sessions):
    # the nightly run records "ran at NIGHT" in maintenance_runs; a leftover row makes the next
    # run of these tests find it not due, so start and end each test with that table empty
    async with sessions.begin() as s:
        await s.execute(_CLEAN)
    yield
    async with sessions.begin() as s:
        await s.execute(_CLEAN)


class FakeLog:
    def __init__(self, msgs):
        self.msgs = msgs
        self.calls = 0

    async def messages_since(self, app, since):
        self.calls += 1
        return self.msgs


async def row(sessions, job_id, scorer):
    async with sessions() as s:
        return await s.scalar(select(Score).where(Score.job_id == job_id, Score.scorer == scorer))


async def test_migration_up_down(pg_url):
    from alembic import command

    engine = make_engine(pg_url)
    cfg = alembic_config(pg_url)

    async def has() -> bool:
        async with engine.connect() as c:
            return await c.scalar(text("select to_regclass('app.maintenance_runs')")) is not None

    try:
        assert await has()
        await engine.dispose()
        command.downgrade(
            cfg, "m10_01_scores"
        )  # the revision just before this table (later ones go too)
        assert not await has()
        command.upgrade(cfg, "head")
        assert await has()
    finally:
        await engine.dispose()


async def test_nightly_scores_reverts_and_refreshes(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    async with sessions.begin() as s:  # scored while delivered
        j = await s.get_one(Job, job.id)
        j.stage = "delivered"
        j.pr_url = "https://github.com/o/r/pull/1"
        j.head_sha = HEAD
        await replace_scores(
            s, job.id, compute_signals(await load_job_facts(s, job.id)), scorers=OBJ_SCORERS
        )
    async with sessions.begin() as s:  # merged later by a human
        j = await s.get_one(Job, job.id)
        j.stage = "merged"
        await record_event(
            s,
            job.id,
            "delivery.merged",
            {"merge_sha": MERGE, "head_sha": HEAD, "pr_url": "https://github.com/o/r/pull/1"},
        )
        await s.execute(
            text("update app.jobs set updated_at = now() + interval '1 minute' where id = :i"),
            {"i": job.id},
        )
    assert (await row(sessions, job.id, "obj.delivery")).label == "delivered"
    log = FakeLog([CommitMessage("f" * 40, f"Revert x\n\nThis reverts commit {MERGE[:9]}.", LATER)])
    logs = {"local": log}
    assert await nightly_scoring(sessions, logs, now=NIGHT) == 1
    assert (await row(sessions, job.id, "obj.delivery")).label == "merged"
    rev = await row(sessions, job.id, "obj.reverted")
    assert (rev.label, rev.passed, rev.score) == ("reverted", False, 0)
    assert await nightly_scoring(sessions, logs, now=NIGHT + timedelta(hours=1)) == 0
    assert log.calls == 1


async def test_not_reverted_and_old_merges_skipped(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    async with sessions.begin() as s:
        (await s.get_one(Job, job.id)).stage = "merged"
        await record_event(
            s, job.id, "delivery.merged", {"merge_sha": MERGE, "head_sha": HEAD, "pr_url": None}
        )
    logs = {"local": FakeLog([CommitMessage("f" * 40, "unrelated", LATER)])}
    assert await nightly_scoring(sessions, logs, now=NIGHT) == 1
    r = await row(sessions, job.id, "obj.reverted")
    assert (r.label, r.score) == ("not_reverted", 1)
    # outside the window: no row is touched
    async with sessions.begin() as s:
        await s.execute(text("delete from app.scores"))
        await s.execute(text("update app.job_events set at = at - interval '40 days'"))
    assert await nightly_scoring(sessions, logs, now=NIGHT + timedelta(days=1)) == 0
    assert await row(sessions, job.id, "obj.reverted") is None
    async with sessions() as s:
        assert await s.scalar(select(JobEvent.id).limit(1)) is not None
