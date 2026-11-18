from dataclasses import replace

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.db.tables import App, Command, DefinitionRow, Job, JobEvent
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.store import DefinitionStore, NoActiveDefinition
from swfactory.domain.types import FactoryMode
from swfactory.settings import get_settings

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _clear_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def ok_result(tmp_path):
    init_definition_repo(tmp_path)
    r = load_definition(str(tmp_path))
    assert not r.errors and r.definition is not None
    return r


def bad_result(tmp_path):
    init_definition_repo(tmp_path)
    p = tmp_path / "factory.yaml"
    p.write_text(p.read_text() + "\nmerge_polcy: x\n")
    r = load_definition(str(tmp_path))
    assert r.errors
    return r


def _variant(r, sha, apps):
    fac = r.definition.factory.model_copy(update={"apps": apps})
    return replace(r, sha=sha, definition=r.definition.model_copy(update={"factory": fac}))


def without_demo(r):
    return _variant(r, "sha-without-demo", {})


def with_demo(r):
    return _variant(r, "sha-with-demo", dict(r.definition.factory.apps))


@pytest.fixture
def sm(sessions):
    return sessions


async def row(sessions, sha):
    async with sessions() as s:
        return await s.get(DefinitionRow, sha)


async def events(sessions, kind):
    async with sessions() as s:
        rows = await s.scalars(
            select(JobEvent.data).where(JobEvent.kind == kind).order_by(JobEvent.id)
        )
        return list(rows)


async def seed_job(sessions, slug, stage):
    async with sessions() as s, s.begin():
        a = await s.scalar(select(App).where(App.slug == slug))
        n = len((await s.scalars(select(Job.id))).all()) + 1
        j = Job(
            app_id=a.id,
            seq=n,
            slug=f"j{n}",
            title="t",
            feature="f",
            requested_by="me",
            budget_usd=1,
            stage=stage,
        )
        s.add(j)
        await s.flush()
        return j.id


async def job_stage(sessions, jid):
    async with sessions() as s:
        return await s.scalar(select(Job.stage).where(Job.id == jid))


async def command_rows(sessions):
    async with sessions() as s:
        rs = await s.execute(select(Command.job_id, Command.action, Command.requested_by))
        return [tuple(r) for r in rs]


async def test_valid_load_becomes_active(sessions, tmp_path):
    store = DefinitionStore(sessions)
    ok = ok_result(tmp_path)
    assert await store.save(ok) is True and (await store.active())[0] == ok.sha
    assert await events(sessions, "definition.loaded") == [
        {"sha": ok.sha, "source": ok.source, "apps": {"demo": "added"}}
    ]
    assert await store.get(ok.sha) is not None and await store.get("nope") is None


async def test_no_active_raises(sessions):
    with pytest.raises(NoActiveDefinition):
        await DefinitionStore(sessions).active()


async def test_invalid_keeps_previous_active(sessions, tmp_path):
    store = DefinitionStore(sessions)
    ok, bad = ok_result(tmp_path / "ok"), bad_result(tmp_path / "bad")
    await store.save(ok)
    assert await store.save(bad) is False
    assert (await store.active())[0] == ok.sha
    r = await row(sessions, bad.sha)
    assert r.active is False and r.errors == [str(e) for e in bad.errors] != []


async def test_sync_apps_anchors_ids(sessions, tmp_path):
    store = DefinitionStore(sessions)
    ok = ok_result(tmp_path)
    assert ok.definition
    gone, back = without_demo(ok).definition, with_demo(ok).definition
    assert gone and back
    async with sessions() as s, s.begin():
        assert await store.sync_apps(s, ok.definition) == {"demo": "added"}
        app_id = await s.scalar(select(App.id).where(App.slug == "demo"))
        assert await store.sync_apps(s, gone) == {"demo": "archived"}
        assert await store.sync_apps(s, back) == {"demo": "updated"}
        a = await s.scalar(select(App).where(App.slug == "demo"))
        assert (a.id, a.archived_at) == (app_id, None)
        assert await store.sync_apps(s, ok.definition) == {"demo": "unchanged"}


async def test_archiving_an_app_stops_its_jobs(sessions, tmp_path):
    store = DefinitionStore(sessions)
    ok = ok_result(tmp_path)
    await store.save(ok)
    q = await seed_job(sessions, "demo", "queued")
    b = await seed_job(sessions, "demo", "build")
    d = await seed_job(sessions, "demo", "delivered")
    await store.save(without_demo(ok))
    assert [await job_stage(sessions, j) for j in (q, b, d)] == ["cancelled", "build", "delivered"]
    assert await command_rows(sessions) == [(b, "cancel", "definition")]
    assert {"stage": "cancelled", "reason": "app archived"} in await events(sessions, "job.stage")


async def test_server_mode_never_activates_dirty(sessions, tmp_path):
    r = replace(ok_result(tmp_path), sha="dirty-0123456789ab")
    assert await DefinitionStore(sessions, mode=FactoryMode.SERVER).save(r) is False
    assert (await row(sessions, "dirty-0123456789ab")).active is False


def test_cli_def_sync_and_apps_list(pg_url, sessions, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    sha = init_definition_repo(tmp_path / "d")
    r = CliRunner().invoke(app, ["def", "sync", str(tmp_path / "d")])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == f"active definition {sha[:12]}; apps: demo (added)"
    rows = CliRunner().invoke(app, ["apps", "list"]).stdout.splitlines()
    assert [ln.split() for ln in rows] == [["demo", "local", "local_merge", "team-gemini"]]
