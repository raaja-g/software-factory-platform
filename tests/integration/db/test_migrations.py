import json
import secrets

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine
from tests.support.pg import execute, libpq, scalar

from swfactory.db.migrate import include_name, include_object, upgrade_database
from swfactory.db.tables import Base

pytestmark = pytest.mark.db


def _compare(url: str) -> list:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn,
                opts={
                    "target_metadata": Base.metadata,
                    "version_table_schema": "app",
                    "include_schemas": True,
                    "include_name": include_name,
                    "include_object": include_object,
                },
            )
            return compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()


async def compare_to_models(url: str) -> list:
    return _compare(url)


async def seed_job(url: str) -> str:
    await execute(
        url,
        "insert into app.apps (slug, repo_kind, repo_url) values ('demo', 'local', '/tmp/demo') "
        "on conflict do nothing",
    )
    await execute(
        url,
        "insert into app.jobs (app_id, seq, slug, title, feature, requested_by, budget_usd) "
        "select id, coalesce((select max(seq) from app.jobs), 0) + 1, 's', 't', 'f', 'me', 10 "
        "from app.apps where slug = 'demo'",
    )
    return await scalar(url, "select id from app.jobs order by seq desc limit 1")


async def test_upgrade_creates_schema(pg_url):
    assert (
        await scalar(pg_url, "select version_num from app.alembic_version")
        == "0006_run_cost_nullable"
    )
    assert await scalar(pg_url, "select to_regclass('app.artifacts_latest')") is not None
    assert await scalar(pg_url, "select count(*) from pg_namespace where nspname = 'lg'") == 1
    assert upgrade_database(pg_url) == "0006_run_cost_nullable"  # idempotent


async def test_backlog_tables_columns_and_turn_function(pg_url):
    assert await scalar(pg_url, "select to_regclass('app.backlogs')") is not None
    assert await scalar(pg_url, "select to_regprocedure('app.backlog_turn(app.jobs)')") is not None
    assert (
        await scalar(
            pg_url,
            "select count(*) from information_schema.columns where table_schema = 'app' "
            "and table_name = 'jobs' "
            "and column_name in ('backlog_id', 'backlog_pos', 'backlog_skipped')",
        )
        == 3
    )
    assert (
        await scalar(
            pg_url,
            "select count(*) from pg_indexes where schemaname = 'app' "
            "and indexdef like 'CREATE UNIQUE%(backlog_id, backlog_pos)%'",
        )
        == 1
    )
    assert (
        await scalar(
            pg_url,
            "select count(*) from pg_indexes where schemaname = 'app' "
            "and indexdef like 'CREATE UNIQUE%(app_id, seq)%' and tablename = 'backlogs'",
        )
        == 1
    )
    with pytest.raises(Exception, match="backlogs_status_check"):
        await execute(
            pg_url,
            "insert into app.backlogs (app_id, seq, title, status, created_by) "
            "(select id, 1, 't', 'bogus', 'me' from app.apps limit 1) "
            "union all select null, 1, 't', 'bogus', 'me'",
        )


async def test_backlog_turn_orders_jobs(pg_url):
    await execute(pg_url, "delete from app.jobs; delete from app.backlogs")
    first = await seed_job(pg_url)
    second = await seed_job(pg_url)
    await execute(
        pg_url,
        "insert into app.backlogs (id, app_id, seq, title, created_by) "
        "select '00000000-0000-7000-8000-000000000001', id, 1, 'b', 'me' from app.apps "
        "where slug = 'demo'",
    )
    await execute(
        pg_url,
        "update app.jobs set backlog_id = '00000000-0000-7000-8000-000000000001', "
        f"backlog_pos = case id when '{first}' then 1 else 2 end",
    )
    turn = "select app.backlog_turn(j) from app.jobs j where id = '{}'"
    assert await scalar(pg_url, turn.format(first)) is True
    assert await scalar(pg_url, turn.format(second)) is False
    await execute(pg_url, f"update app.jobs set stage = 'merged' where id = '{first}'")
    assert await scalar(pg_url, turn.format(second)) is True
    await execute(pg_url, "update app.backlogs set status = 'cancelled'")
    assert await scalar(pg_url, turn.format(second)) is False


async def test_migration_matches_models(pg_url):
    await execute(pg_url, "create table if not exists lg.checkpoints (thread_id text primary key)")
    assert await compare_to_models(pg_url) == []


async def test_insert_notifies(pg_url):
    job_id = await seed_job(pg_url)
    async with await psycopg.AsyncConnection.connect(libpq(pg_url), autocommit=True) as conn:
        await conn.execute("LISTEN job_events")
        await conn.execute("LISTEN commands")
        await execute(
            pg_url,
            f"insert into app.job_events (job_id, kind, data) "
            f"values ('{job_id}', 'job.stage', '{{}}')",
        )
        await execute(
            pg_url,
            f"insert into app.commands (job_id, action, requested_by) "
            f"values ('{job_id}', 'approve', 'me')",
        )
        ns = [n async for n in conn.notifies(timeout=2, stop_after=2)]
    assert sorted(n.channel for n in ns) == ["commands", "job_events"]
    assert all(
        set(json.loads(n.payload)) == {"id", "job_id"}
        and json.loads(n.payload)["job_id"] == str(job_id)
        for n in ns
    )


async def test_least_privilege_roles(pg_url):
    def q(sql):
        return scalar(pg_url, sql)

    assert (
        await q("select has_schema_privilege('factory_app', 'app', 'USAGE')"),
        await q("select has_schema_privilege('factory_app', 'app', 'CREATE')"),
        await q("select has_schema_privilege('factory_app', 'lg', 'USAGE')"),
    ) == (True, False, False)
    assert await q(
        "select array_agg(p order by p) from unnest(array['SELECT','INSERT','UPDATE','DELETE']) p "
        "where has_table_privilege('factory_app', 'app.jobs', p)"
    ) == ["DELETE", "INSERT", "SELECT", "UPDATE"]
    assert (
        await q("select has_schema_privilege('factory_graph', 'lg', 'CREATE')"),
        await q("select has_schema_privilege('factory_graph', 'app', 'USAGE')"),
    ) == (True, False)
    assert "search_path=lg" in await q(
        "select array_to_string(rolconfig, ',') from pg_roles where rolname = 'factory_graph'"
    )
    probe = f"probe_{secrets.token_hex(4)}"
    await execute(pg_url, f"create role {probe} login")
    try:
        assert (
            await q(f"select has_database_privilege('{probe}', current_database(), 'CONNECT')")
            is False
        )
    finally:
        await execute(pg_url, f"drop role {probe}")
