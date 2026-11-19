import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from swfactory.cli.main import app
from swfactory.db.tables import App, JobEvent
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import AppDef
from swfactory.definition.store import DefinitionStore, NoActiveDefinition
from swfactory.domain.types import JobKind
from swfactory.queue import UnknownApp, claim_next_job, enqueue_job, release_job, renew_lease

pytestmark = pytest.mark.db


async def activate(sessions, apps):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    base = next(iter(r.definition.factory.apps.values()))
    defs = {
        k: AppDef.model_validate({**base.model_dump(mode="json"), **v}) for k, v in apps.items()
    }
    fac = r.definition.factory.model_copy(update={"apps": defs})
    sha = "sha-" + "-".join(sorted(apps)) + str(datetime.now(UTC).timestamp())
    r = replace(r, sha=sha, definition=r.definition.model_copy(update={"factory": fac}))
    assert await DefinitionStore(sessions).save(r)
    return sha


async def enq(sessions, app="a", feature="Add a /health endpoint"):
    async with sessions.begin() as s:
        return await enqueue_job(
            s,
            app_slug=app,
            feature=feature,
            title=None,
            source="cli",
            source_ref=None,
            requested_by="me",
        )


async def claim(sessions, w, lease_s=60) -> Any:
    async with sessions() as s:
        return await claim_next_job(s, worker_id=w, lease_s=lease_s)


async def archive(sessions, slug):
    async with sessions.begin() as s:
        a = await s.scalar(select(App).where(App.slug == slug))
        a.archived_at = datetime.now(UTC)


async def job_events(sessions, kind):
    async with sessions() as s:
        return list(
            (
                await s.scalars(
                    select(JobEvent.data).where(JobEvent.kind == kind).order_by(JobEvent.id)
                )
            ).all()
        )


async def test_enqueue_fills_from_definition(sessions):
    sha = await activate(sessions, apps={"demo": {}})
    j = await enq(sessions, app="demo")
    assert (
        j.stage,
        j.seq,
        j.round,
        j.max_rounds,
        j.budget_usd,
        j.definition_sha,
        j.graph_version,
    ) == (
        "queued",
        1,
        0,
        2,
        Decimal("10"),
        sha,
        None,
    )
    assert (j.branch, j.title) == ("factory/001-add-a-health-endpoint", "Add a /health endpoint")
    assert await job_events(sessions, "job.created") == [
        {"app": "demo", "seq": 1, "title": "Add a /health endpoint", "source": "cli"}
    ]


async def test_enqueue_without_active_definition(sessions):
    with pytest.raises(NoActiveDefinition):
        await enq(sessions, app="demo")


async def test_concurrent_enqueue_unique_seq(sessions):
    await activate(sessions, apps={"demo": {}})
    jobs = await asyncio.gather(*(enq(sessions, app="demo", feature=f"f{i}") for i in range(10)))
    assert sorted(j.seq for j in jobs) == list(range(1, 11))


async def test_enqueue_joins_the_callers_transaction(sessions):
    await activate(sessions, apps={"demo": {}})
    with pytest.raises(RuntimeError, match="caller failed"):
        async with sessions.begin() as s:
            await enqueue_job(
                s,
                app_slug="demo",
                feature="f",
                title=None,
                source="api",
                source_ref="r1",
                requested_by="me",
            )
            raise RuntimeError("caller failed after the enqueue")
    async with sessions() as s:
        assert await s.scalar(text("select count(*) from app.jobs")) == 0


async def test_enqueue_rejects(sessions):
    await activate(sessions, apps={"demo": {"allowed_profiles": ["team-gemini"]}, "old": {}})
    await archive(sessions, "old")
    for slug in ("nope", "old"):
        with pytest.raises(UnknownApp):
            await enq(sessions, app=slug)
    with pytest.raises(ValueError, match="profile 'local' is not allowed for app 'demo'"):
        async with sessions() as s:
            await enqueue_job(
                s,
                app_slug="demo",
                feature="f",
                title=None,
                source="cli",
                source_ref=None,
                requested_by="me",
                profile_name="local",
            )


async def test_two_concurrent_claimers_two_apps(sessions):
    await activate(sessions, apps={"a": {}, "b": {}})
    ja, jb = await enq(sessions, app="a"), await enq(sessions, app="b")
    got = await asyncio.gather(claim(sessions, "w1"), claim(sessions, "w2"))
    assert {g.id for g in got} == {ja.id, jb.id} and {g.lease_owner for g in got} == {"w1", "w2"}


async def test_per_app_limit_of_one(sessions):
    await activate(sessions, apps={"a": {}})
    await enq(sessions, app="a", feature="f1")
    await enq(sessions, app="a", feature="f2")
    got = [g for g in await asyncio.gather(claim(sessions, "w1"), claim(sessions, "w2")) if g]
    assert len(got) == 1 and await claim(sessions, "w3") is None
    async with sessions() as s:
        await release_job(
            s, job_id=got[0].id, worker_id=got[0].lease_owner, lease_epoch=got[0].lease_epoch
        )
    nxt = await claim(sessions, "w4")
    assert nxt is not None and nxt.app_id == got[0].app_id


async def test_eight_claimers_three_apps(sessions):
    await activate(sessions, apps={k: {} for k in "abc"})
    for k in "abc":
        for i in range(3):
            await enq(sessions, app=k, feature=f"{k}{i}")
    got = [g for g in await asyncio.gather(*(claim(sessions, f"w{i}") for i in range(8))) if g]
    assert len(got) == 3 and len({g.app_id for g in got}) == 3


async def test_app_concurrency_override(sessions):
    await activate(sessions, apps={"a": {"concurrency": 2}})
    for i in range(3):
        await enq(sessions, app="a", feature=f"f{i}")
    got = [g for g in await asyncio.gather(*(claim(sessions, f"w{i}") for i in range(3))) if g]
    assert len(got) == 2


async def test_one_apps_backlog_does_not_hide_another_apps_job(sessions):
    await activate(sessions, apps={"a": {}, "b": {}})
    for i in range(22):
        await enq(sessions, app="a", feature=f"a{i}")
    jb = await enq(sessions, app="b")
    first = await claim(sessions, "w1")
    assert first.app_id != jb.app_id
    assert (await claim(sessions, "w2")).id == jb.id and await claim(sessions, "w3") is None


async def test_archived_apps_jobs_are_never_candidates(sessions):
    await activate(sessions, apps={"a": {}, "b": {}})
    await enq(sessions, app="a")
    jb = await enq(sessions, app="b")
    await archive(sessions, "a")
    assert (await claim(sessions, "w1")).id == jb.id and await claim(sessions, "w2") is None


async def _bench(sessions):
    async with sessions.begin() as s:
        return await enqueue_job(
            s,
            app_slug="demo",
            feature="bench task",
            title=None,
            source="api",
            source_ref=None,
            requested_by="bench",
            kind=JobKind.BENCHMARK,
        )


async def test_benchmark_job_does_not_take_app_slot(sessions):
    await activate(sessions, apps={"demo": {}})
    b = await _bench(sessions)
    assert (await claim(sessions, "w1")).id == b.id
    n = await enq(sessions, app="demo")
    got = await claim(sessions, "w2")
    assert got is not None and (got.id, got.kind) == (n.id, "normal")


async def test_benchmark_claimable_while_normal_holds_slot(sessions):
    await activate(sessions, apps={"demo": {}})
    n = await enq(sessions, app="demo")
    assert (await claim(sessions, "w1")).id == n.id
    b = await _bench(sessions)
    got = await claim(sessions, "w2")
    assert got is not None and (got.id, got.kind) == (b.id, "benchmark")


async def test_lease_renew_and_expiry(sessions):
    await activate(sessions, apps={"a": {}})
    await enq(sessions, app="a")
    j = await claim(sessions, "w1", lease_s=1)
    async with sessions() as s:
        assert (
            await renew_lease(s, job_id=j.id, worker_id="w1", lease_epoch=j.lease_epoch, lease_s=1)
            is True
        )
    async with sessions() as s:
        assert (
            await renew_lease(s, job_id=j.id, worker_id="w2", lease_epoch=j.lease_epoch, lease_s=1)
            is False
        )
    assert await claim(sessions, "w2") is None
    await asyncio.sleep(1.5)
    again = await claim(sessions, "w2")
    assert (again.id, again.lease_owner) == (j.id, "w2")


def test_cli_jobs_create_and_list(pg_url, sessions, tmp_path, monkeypatch, api_env):
    init_definition_repo(tmp_path / "d")
    assert CliRunner().invoke(app, ["def", "sync", str(tmp_path / "d")]).exit_code == 0
    r = CliRunner().invoke(app, ["jobs", "create", "--app", "demo", "Add a /health endpoint"])
    first, link = r.stdout.strip().splitlines()  # the reference line, then the dashboard link
    assert r.exit_code == 0 and re.fullmatch(r"queued demo:001 ([0-9a-f-]{36})", first)
    assert link.endswith("/#/job/" + first.split()[-1])
    listed = CliRunner().invoke(app, ["jobs", "list"]).stdout.split()
    assert listed[:6] == [
        "demo:001",
        "queued",
        "Add",
        "a",
        "/health",
        "endpoint",
    ]  # ref, stage, title
