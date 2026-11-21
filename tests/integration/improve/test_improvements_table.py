from __future__ import annotations

import tempfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.support.pg import execute, scalar

from swfactory.db.migrate import alembic_config
from swfactory.db.tables import IMPROVEMENT_DONE, IMPROVEMENT_STATUSES, Improvement, Job
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.store import DefinitionStore
from swfactory.queue import claim_next_job, enqueue_job

pytestmark = pytest.mark.db


async def insert_improvement(s, **kw) -> Improvement:
    row = Improvement(
        **{
            "trigger": "manual",
            "role_group": "builder",
            "base_sha": "a" * 40,
            "suite": "v1-parity",
            **kw,
        }
    )
    s.add(row)
    await s.flush()
    await s.refresh(row)
    return row


async def insert_job(s, *, kind: str, app_id=None, seq: int = 1, **kw) -> Job:
    row = Job(
        app_id=app_id,
        seq=seq,
        slug=f"j{seq}",
        title="t",
        feature="f",
        kind=kind,
        requested_by="me",
        branch=f"b{seq}",
        budget_usd=Decimal("1"),
        definition_sha="x",
        profile_name="team-gemini",
        **kw,
    )
    s.add(row)
    await s.flush()
    return row


def test_status_constants():
    assert IMPROVEMENT_DONE < set(IMPROVEMENT_STATUSES)
    assert "queued" in IMPROVEMENT_STATUSES and "queued" not in IMPROVEMENT_DONE


async def test_columns_and_checks(sessions):
    async with sessions.begin() as s:
        row = await insert_improvement(s, trigger="age")
        assert row.cost_usd == Decimal("0") and row.evidence == {} and row.status == "queued"
        assert row.job_id is None and row.cycle_id is None and row.proposed_sha is None
        assert row.error is None and row.updated_at is not None
        assert (await insert_job(s, kind="improver", app_id=None)).app_id is None
    for bad in ({"status": "bogus"}, {"trigger": "bogus"}):
        with pytest.raises(IntegrityError):
            async with sessions.begin() as s:
                await insert_improvement(s, **bad)
    with pytest.raises(IntegrityError):
        async with sessions.begin() as s:
            await insert_job(s, kind="normal", app_id=None, seq=2)


async def _activate(sessions) -> None:
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    r = replace(r, sha="m12-claim-sha")
    assert await DefinitionStore(sessions).save(r)


async def test_claim_skips_improver_jobs(sessions):
    await _activate(sessions)
    async with sessions.begin() as s:
        old = await insert_job(s, kind="improver", app_id=None)
        old2 = await insert_job(s, kind="improver", app_id=None, seq=2, stage="build")
        await s.execute(
            text(
                "UPDATE app.jobs SET lease_owner='dead', lease_until=now() - interval '1 hour' "
                "WHERE id = :i"
            ),
            {"i": old2.id},
        )
        assert old.app_id is None
    async with sessions.begin() as s:
        slug = next(iter(await _slugs(s)))
        await enqueue_job(
            s,
            app_slug=slug,
            feature="Add a /health endpoint",
            title=None,
            source="cli",
            source_ref=None,
            requested_by="me",
        )
    async with sessions() as s:
        first = await claim_next_job(s, worker_id="w1", lease_s=90)
    assert first is not None and first.kind == "normal"
    async with sessions() as s:
        assert await claim_next_job(s, worker_id="w2", lease_s=90) is None
    async with sessions() as s:
        untouched = await s.scalar(
            text("select count(*) from app.jobs where kind = 'improver' and lease_epoch = 0")
        )
    assert untouched == 2
    async with sessions() as s:
        await s.execute(text("truncate app.jobs cascade"))
        await s.commit()


async def _slugs(s):
    from swfactory.definition.store import load_active

    found = await load_active(s)
    assert found is not None
    return list(found[1].factory.apps)


async def test_migration_down_up(pg_url):
    cfg = alembic_config(pg_url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head
    try:
        command.downgrade(cfg, "m11_02_laya_datasets")
        assert (
            await scalar(
                pg_url,
                "select count(*) from information_schema.columns where table_schema='app' "
                "and table_name='improvements' and column_name='cycle_id'",
            )
            == 0
        )
    finally:
        command.upgrade(cfg, "head")
    assert (
        await scalar(
            pg_url,
            "select count(*) from information_schema.columns where table_schema='app' "
            "and table_name='improvements' and column_name='cycle_id'",
        )
        == 1
    )
    await execute(pg_url, "select 1")
