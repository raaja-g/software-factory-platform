"""M11.8: fitting heads in the router with a 50-row floor and versioned storage."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy import select, text
from tests.support.pg import truncate_app_tables
from tests.unit.cli.conftest import run
from tests.unit.laya.test_export import (
    STATE,
    BenchResult,
    BenchRun,
    correct,
    db,  # noqa: F401  (fixture)
    decision,
    make_app,
    make_job,
)

from swfactory.db.engine import make_engine, make_sessionmaker
from swfactory.db.tables import LayaHead
from swfactory.laya import fit as fit_mod
from swfactory.laya.fit import (
    MIN_FIT_ROWS,
    NotEnoughRows,
    RouterUnreachable,
    fit_question,
    questions_sha256,
)
from swfactory.models.triage import TRIAGE_QUESTIONS
from swfactory.settings import get_settings

URL = "http://router:8000"
SENT: list[dict] = []


def _fake(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    SENT.append(body)
    assert request.headers["authorization"] == "Bearer k"
    return httpx.Response(
        200,
        json={
            "head_version": f"{body['question_id']}@v{body['version']}",
            "metrics": {"rows": 60, "accuracy": 0.9},
            "head_sha256": "ab" * 32,
        },
        headers={"x-factory-router-version": "laya 0.4.31"},
    )


FAKE_FIT = httpx.MockTransport(_fake)


async def seed_touches_ui(s, n: int) -> None:
    a = await make_app(s)
    for i in range(n):
        await correct(s, await make_job(s, a, i), {"touches_ui": i % 2 == 0, "by": "a", "at": "t"})


async def seed_tier(s, n: int, classes: dict[str, int] | None = None, bench: int = 0):
    a = await make_app(s)
    run_ = BenchRun(suite="mine", configurations=[])
    s.add(run_)
    await s.flush()
    labels = [
        lab for lab, k in (classes or {"A": n // 2, "B": n - n // 2}).items() for _ in range(k)
    ]
    jobs = []
    for i, lab in enumerate(labels):
        j = await make_job(s, a, i)
        jobs.append(j)
        if i < bench:
            s.add(
                BenchResult(
                    run_id=run_.id, task=f"{i:03d}-todo-web", configuration="c", job_id=j.id
                )
            )
            await decision(s, j, lab, source="bench", state={**STATE, "i": i})
        else:
            await decision(s, j, lab, state={**STATE, "i": i})
    return jobs


@pytest.fixture(autouse=True)
def _reset():
    SENT.clear()


@pytest.fixture
def tier_def(monkeypatch):
    async def qd(s, question):
        return {"type": "choice", "instructions": "x", "criteria": {"A": "a", "B": "b"}}

    monkeypatch.setattr(fit_mod, "question_definition", qd)


def test_floors():
    assert (MIN_FIT_ROWS, fit_mod.MIN_ROWS_PER_CLASS) == (50, 10)


def test_questions_sha256_is_canonical():
    a = questions_sha256({"b": 1, "a": {"y": 1, "x": 2}})
    assert a == questions_sha256({"a": {"x": 2, "y": 1}, "b": 1}) and len(a) == 64
    assert a != questions_sha256({"a": 1})


@pytest.mark.db
async def test_refuses_below_50(db):  # noqa: F811
    async with db() as s, s.begin():
        await seed_touches_ui(s, 49)
        with pytest.raises(
            NotEnoughRows, match="49 labelled rows for touches_ui, need at least 50"
        ):
            await fit_question(s, URL, "k", "touches_ui")
    assert SENT == []


@pytest.mark.db
async def test_versions_increment(db):  # noqa: F811
    async with db() as s, s.begin():
        await seed_touches_ui(s, 60)
        await fit_question(s, URL, "k", "touches_ui", transport=FAKE_FIT)
        h = await fit_question(s, URL, "k", "touches_ui", transport=FAKE_FIT)
        assert (h.version, h.active, h.rows, len(h.head_sha256)) == (2, False, 60, 64)
        active = list(await s.scalars(select(LayaHead).where(LayaHead.active.is_(True))))
        assert active == []
        assert [b["version"] for b in SENT] == [1, 2]
        assert h.questions_sha256 == questions_sha256(TRIAGE_QUESTIONS["touches_ui"])
        assert h.router_version == "laya 0.4.31" and h.path == "/heads/touches_ui/v2"
        assert h.label_counts == {"A": 30, "B": 30}
        assert SENT[0]["csv"].splitlines()[0] == "state,touches_ui"


@pytest.mark.db
async def test_refuses_one_label_class(db, tier_def):  # noqa: F811
    async with db() as s, s.begin():
        await seed_tier(s, 60, classes={"C": 60})
        with pytest.raises(
            NotEnoughRows,
            match="tier.builder has 1 label class, need at least 2 with 10 rows each",
        ):
            await fit_question(s, URL, "k", "tier.builder")
    assert SENT == []


@pytest.mark.db
async def test_refuses_thin_second_class(db, tier_def):  # noqa: F811
    async with db() as s, s.begin():
        await seed_tier(s, 60, classes={"A": 55, "B": 5})
        with pytest.raises(NotEnoughRows, match="2 label classes, need at least 2 with 10"):
            await fit_question(s, URL, "k", "tier.builder")


@pytest.mark.db
async def test_fit_records_training_refs(db, tier_def):  # noqa: F811
    async with db() as s, s.begin():
        await seed_tier(s, 60, bench=20)
        h = await fit_question(s, URL, "k", "tier.builder", transport=FAKE_FIT)
        assert "bench:mine/003-todo-web" in h.train_refs
        assert h.train_refs == sorted(h.train_refs) and len(h.train_refs) == 60
        assert any(r.startswith("job:demo:") for r in h.train_refs)


@pytest.mark.db
async def test_router_down_is_distinct(db):  # noqa: F811
    def boom(request):
        raise httpx.ConnectError("no route")

    async with db() as s, s.begin():
        await seed_touches_ui(s, 60)
        with pytest.raises(RouterUnreachable):
            await fit_question(s, URL, "k", "touches_ui", transport=httpx.MockTransport(boom))
        rows = (await s.execute(text("select count(*) from app.laya_heads"))).scalar()
        assert rows == 0


def _cli_env(pg_url, monkeypatch):
    monkeypatch.setenv("FACTORY_DATABASE_URL", pg_url)
    monkeypatch.setenv("FACTORY_DB_PASSWORD_FILE", "/nonexistent")
    monkeypatch.delenv("FACTORY_LAYA_URL", raising=False)
    get_settings.cache_clear()


async def _with_db(pg_url, fn):
    engine = make_engine(pg_url)
    try:
        async with engine.connect() as conn:
            await truncate_app_tables(conn)
        async with make_sessionmaker(engine)() as s, s.begin():
            await fn(s)
    finally:
        await engine.dispose()


async def _clean(pg_url):
    engine = make_engine(pg_url)
    try:
        async with engine.connect() as conn:
            await truncate_app_tables(conn)
    finally:
        await engine.dispose()


@pytest.mark.db
def test_cli_exit_codes(pg_url, monkeypatch):
    async def seed10(s):
        await seed_touches_ui(s, 10)

    async def seed60(s):
        await seed_touches_ui(s, 60)

    try:
        _cli_env(pg_url, monkeypatch)
        asyncio.run(_with_db(pg_url, seed10))
        r = run("laya", "fit", "--question", "touches_ui")
        assert r.exit_code == 2 and "refused: 10 labelled rows" in r.output
        assert run("laya", "fit", "--question", "colour").exit_code == 2
        asyncio.run(_with_db(pg_url, seed60))
        r = run("laya", "fit", "--question", "touches_ui")  # enough rows, no router configured
        assert r.exit_code == 3, r.output
    finally:
        get_settings.cache_clear()
        asyncio.run(_clean(pg_url))


@pytest.mark.db
def test_cli_status_lists_questions(pg_url, monkeypatch):
    async def seed(s):
        await seed_touches_ui(s, 3)
        s.add(
            LayaHead(
                question="touches_ui", version=1, rows=60, label_counts={}, questions_sha256="q",
                csv_sha256="c", path="/heads/touches_ui/v1", head_sha256="h", active=False,
                train_refs=[], evaluation={"verdict": "pass"},
            )
        )  # fmt: skip

    try:
        _cli_env(pg_url, monkeypatch)
        asyncio.run(_with_db(pg_url, seed))
        r = run("laya", "status")
        assert r.exit_code == 0, r.output
        lines = {ln.split("\t")[0]: ln for ln in r.output.splitlines()}
        assert set(lines) >= {"kind", "size", "risk", "touches_ui"}
        assert "candidate=touches_ui@v1 (eval: pass)" in lines["touches_ui"]
        assert "rows=3" in lines["touches_ui"] and "active=-" in lines["touches_ui"]
    finally:
        get_settings.cache_clear()
        asyncio.run(_clean(pg_url))


@pytest.mark.db
async def test_migration_matches_table(db):  # noqa: F811
    async with db() as s, s.begin():
        cols = (
            await s.execute(
                text(
                    "select column_name from information_schema.columns "
                    "where table_schema='app' and table_name='laya_heads'"
                )
            )
        ).scalars()
        assert {
            "id", "question", "version", "rows", "label_counts", "questions_sha256", "csv_sha256",
            "path", "router_version", "metrics", "active", "head_sha256", "train_refs",
            "evaluation", "activated_by", "activated_at", "created_at", "source_counts",
        } == set(cols)  # fmt: skip
