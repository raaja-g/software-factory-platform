"""M11.7: the state and label CSV export."""

from __future__ import annotations

import csv
import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from tests.support.pg import truncate_app_tables
from tests.unit.cli.conftest import run

from swfactory.db.tables import (
    App,
    Artifact,
    BenchResult,
    BenchRun,
    Job,
    JobEvent,
    ModelDecision,
)
from swfactory.definition.models import Definition
from swfactory.laya.export import export_rows, question_ids, write_csv

SEQ = itertools.count(1)
T0 = datetime(2026, 11, 1, tzinfo=UTC)
STATE = {"app": "demo", "role": "builder", "round": 1, "diff_files": 3}


def test_csv_columns(tmp_path):
    write_csv([('{"title": "a, b"}', "A")], tmp_path / "t.csv", "touches_ui")
    with open(tmp_path / "t.csv") as f:
        assert list(csv.DictReader(f)) == [{"state": '{"title": "a, b"}', "touches_ui": "A"}]


def test_write_csv_counts(tmp_path):
    c = write_csv([("{}", "A"), ("{}", "A"), ("{}", "B")], tmp_path / "x" / "t.csv", "q")
    assert dict(c) == {"A": 2, "B": 1}


def test_cli_unknown_question():
    assert run("laya", "export", "--question", "colour").exit_code == 2
    assert run("laya", "export", "--question", "tier.reviewer-code").exit_code == 2


def _defn(roles: dict) -> Definition:
    from swfactory.definition.models import Profile

    return Definition.model_construct(profiles={"p": Profile.model_validate({"roles": roles})})


def test_tier_questions_only_for_tier_roles():
    tiers = [{"label": "A", "model": "m", "description": "d"}]
    base = {"engine": "fake", "model": "m"}
    d = _defn(
        {
            "builder": {**base, "mode": "laya", "tiers": tiers},
            "reviewer-code": {**base, "tiers": tiers},
        }
    )
    q = question_ids(d)
    assert "tier.builder" in q and "tier.reviewer-code" not in q
    assert q[:4] == ["kind", "size", "risk", "touches_ui"]


@pytest.fixture
async def db(sessions):
    async def clean():
        async with sessions.kw["bind"].connect() as conn:
            await truncate_app_tables(conn)

    await clean()
    yield sessions
    await clean()


async def make_app(s, slug="demo") -> App:
    a = App(slug=slug, repo_kind="local", repo_url=f"/tmp/{slug}")
    s.add(a)
    await s.flush()
    return a


async def make_job(s, a: App, n: int = 0) -> Job:
    j = Job(
        id=uuid.uuid4(), app_id=a.id, seq=next(SEQ), slug="j", title=f"t{n}", feature="f",
        requested_by="me", budget_usd=1, created_at=T0 + timedelta(minutes=n),
    )  # fmt: skip
    s.add(j)
    await s.flush()
    return j


async def correct(s, j: Job, doc: dict, *, event: bool = True) -> None:
    s.add(
        Artifact(job_id=j.id, path="triage-correction.json", kind="json", content=json.dumps(doc))
    )
    if event:
        s.add(
            JobEvent(
                job_id=j.id, kind="human.action",
                data={"action": "correct_triage", "by": "a@x", "status": "requested"},
            )
        )  # fmt: skip
    await s.flush()


async def decision(s, j: Job, label: str | None, *, source="job", state=STATE, role="builder"):
    d = ModelDecision(
        job_id=j.id, role=role, round=1, purpose="model", mode="laya", options=[], chosen="m",
        answer={"state": state} if state is not None else None,
        outcome={"label": label, "source": source},
    )  # fmt: skip
    s.add(d)
    await s.flush()
    return d


@pytest.mark.db
async def test_triage_labels_are_corrections_only(db):
    async with db() as s, s.begin():
        a = await make_app(s)
        j1, j2, j3 = [await make_job(s, a, i) for i in range(3)]
        await correct(s, j1, {"touches_ui": False, "by": "a@x", "at": "t"})
        await correct(s, j2, {"kind": "bugfix", "by": "a@x", "at": "t"})
        assert j3
        ui = await export_rows(s, "touches_ui")
        kind = await export_rows(s, "kind")
        assert [r.label for r in ui] == ["B"] and [r.label for r in kind] == ["bugfix"]
        assert ui[0].source == f"job:demo:{j1.seq:03d}"
        assert json.loads(ui[0].state)["title"] == "t0"  # fallback to triage_state


@pytest.mark.db
async def test_correction_needs_its_event(db):
    async with db() as s, s.begin():
        a = await make_app(s)
        j = await make_job(s, a)
        await correct(s, j, {"touches_ui": False, "by": "a@x", "at": "t"}, event=False)
        assert await export_rows(s, "touches_ui") == []


@pytest.mark.db
async def test_triage_state_is_the_stored_one(db):
    async with db() as s, s.begin():
        a = await make_app(s)
        j = await make_job(s, a)
        await correct(s, j, {"size": "L", "by": "a@x", "at": "t"})
        s.add(
            ModelDecision(
                job_id=j.id, role="triage", purpose="triage", mode="laya", options=[],
                answer={"question": "size", "state": {"title": "stored"}},
            )
        )  # fmt: skip
        await s.flush()
        assert json.loads((await export_rows(s, "size"))[0].state) == {"title": "stored"}


@pytest.mark.db
async def test_state_matches_decision_time(db):
    async with db() as s, s.begin():
        a = await make_app(s)
        j = await make_job(s, a)
        d = await decision(s, j, "B")
        rows = await export_rows(s, "tier.builder")
        assert d.answer is not None and json.loads(rows[0][0]) == d.answer["state"]
        assert rows[0].source == f"job:demo:{j.seq:03d}"


@pytest.mark.db
async def test_unlabelled_skipped(db):
    async with db() as s, s.begin():
        a = await make_app(s)
        await decision(s, await make_job(s, a, 0), None)
        await decision(s, await make_job(s, a, 1), "A", state=None)
        await decision(s, await make_job(s, a, 2), "C")
        rows = await export_rows(s, "tier.builder")
        assert [r.label for r in rows] == ["C"] and all(r.label for r in rows)


@pytest.mark.db
async def test_bench_rows_name_their_task(db):
    async with db() as s, s.begin():
        a = await make_app(s)
        run_ = BenchRun(suite="mine", configurations=[])
        s.add(run_)
        await s.flush()
        j = await make_job(s, a, 0)
        s.add(BenchResult(run_id=run_.id, task="003-todo-web", configuration="c", job_id=j.id))
        await decision(s, j, "B", source="bench")
        await decision(s, await make_job(s, a, 1), "A")
        rows = await export_rows(s, "tier.builder")
        assert {r.source for r in rows if r.source.startswith("bench:")} == {
            "bench:mine/003-todo-web"
        }
        assert [r.label for r in rows] == ["B", "A"]  # ordered by job creation time


@pytest.mark.db
def test_cli_export_writes_csv(pg_url, monkeypatch, tmp_path):
    import asyncio

    from swfactory.db.engine import make_engine, make_sessionmaker
    from swfactory.settings import get_settings

    async def seed():
        engine = make_engine(pg_url)
        try:
            async with engine.connect() as conn:
                await truncate_app_tables(conn)
            async with make_sessionmaker(engine)() as s, s.begin():
                a = await make_app(s)
                await decision(s, await make_job(s, a, 0), "A")
                await decision(s, await make_job(s, a, 1), "B")
        finally:
            await engine.dispose()

    async def clean():
        engine = make_engine(pg_url)
        try:
            async with engine.connect() as conn:
                await truncate_app_tables(conn)
        finally:
            await engine.dispose()

    asyncio.run(seed())
    try:
        monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
        monkeypatch.setenv("FACTORY_DB_PASSWORD_FILE", "/nonexistent")
        get_settings.cache_clear()
        out = tmp_path / "o" / "b.csv"
        r = run("laya", "export", "--question", "tier.builder", "--out", str(out))
        assert r.exit_code == 0, r.output
        assert f"wrote {out}: 2 rows (A: 1, B: 1)" in r.output
        with open(out) as f:
            assert [x["tier.builder"] for x in csv.DictReader(f)] == ["A", "B"]
    finally:
        get_settings.cache_clear()
        asyncio.run(clean())


@pytest.mark.db
def test_cli_export_uses_the_export_dir_setting_and_reports_unwritable_dirs(
    pg_url, monkeypatch, tmp_path
):
    """In the container the cwd is not writable, so the output folder is a setting
    (FACTORY_LAYA_EXPORT_DIR). An unwritable folder is a clean error, not a traceback."""
    import asyncio

    from swfactory.db.engine import make_engine
    from swfactory.settings import get_settings

    async def clean():
        engine = make_engine(pg_url)
        try:
            async with engine.connect() as conn:
                await truncate_app_tables(conn)
        finally:
            await engine.dispose()

    asyncio.run(clean())
    try:
        monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
        monkeypatch.setenv("FACTORY_DB_PASSWORD_FILE", "/nonexistent")
        monkeypatch.setenv("FACTORY_LAYA_EXPORT_DIR", str(tmp_path / "exports"))
        get_settings.cache_clear()
        ok = run("laya", "export", "--question", "touches_ui")
        assert ok.exit_code == 0, ok.output
        assert (tmp_path / "exports" / "touches_ui.csv").is_file()
        blocker = tmp_path / "file-not-dir"
        blocker.write_text("x")
        monkeypatch.setenv("FACTORY_LAYA_EXPORT_DIR", str(blocker / "sub"))
        get_settings.cache_clear()
        bad = run("laya", "export", "--question", "touches_ui")
        assert bad.exit_code == 1 and "cannot write" in bad.output
        assert "Traceback" not in bad.output
    finally:
        get_settings.cache_clear()
        asyncio.run(clean())
