"""M11.10: importing synthetic training rows tagged external."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support.pg import truncate_app_tables
from tests.unit.cli.conftest import run
from tests.unit.laya.test_export import db, make_app  # noqa: F401  (fixture)
from tests.unit.laya.test_fit import FAKE_FIT, URL, _clean, _cli_env, seed_touches_ui

from swfactory.db.engine import make_engine
from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.laya.evaluate import ActivationRefused, activate_head, holdout_overlap
from swfactory.laya.export import ExportRow, export_rows, source_counts
from swfactory.laya.fit import NotEnoughRows, fit_question
from swfactory.laya.import_rows import ImportRefused, import_rows, read_import_file
from swfactory.settings import get_settings

FIXTURE = Path(__file__).parents[2] / "fixtures" / "laya" / "synthetic_touches_ui.csv"
_DEFN = load_definition(str(defaults_dir())).definition
assert _DEFN is not None
DEFN = _DEFN


def csv_with(tmp_path: Path, n: int, split: tuple[int, int] | None = None) -> Path:
    a, b = split or (n, 0)
    lines = ["state,touches_ui"]
    for i in range(a + b):
        lab = "A" if i < a else "B"
        lines.append('"' + json.dumps({"title": f"t{i}-{n}-{a}"}).replace('"', '""') + f'",{lab}')
    f = tmp_path / f"r{n}-{a}.csv"
    f.write_text("\n".join(lines) + "\n")
    return f


def csv_bad_label(tmp_path: Path) -> Path:
    f = tmp_path / "bad.csv"
    f.write_text('state,touches_ui\n"{}",A\n"{}",Z\n')
    return f


async def count_rows(s, table: str) -> int:
    return (await s.execute(text(f"select count(*) from app.{table}"))).scalar_one()


def test_read_valid_file(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text(
        'state,touches_ui\n"{""title"": ""dark mode""}",A\n'
        '"{""title"": ""fix typo in README""}",B\n'
    )
    assert read_import_file(f, "touches_ui", {"A", "B"}) == [
        ('{"title": "dark mode"}', "A"),
        ('{"title": "fix typo in README"}', "B"),
    ]


def test_shipped_fixture_is_valid():
    assert len(read_import_file(FIXTURE, "touches_ui", {"A", "B"})) == 24


def test_refuses_wrong_header(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text('state,colour\n"{}",A\n')
    with pytest.raises(ImportRefused, match="header must be state,touches_ui"):
        read_import_file(f, "touches_ui", {"A", "B"})


def test_refuses_unknown_label_naming_the_line(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text('state,touches_ui\n"{}",A\n"{}",Z\n')
    with pytest.raises(ImportRefused, match="line 3: label Z is not one of A, B"):
        read_import_file(f, "touches_ui", {"A", "B"})


def test_refuses_state_that_is_not_a_json_object(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text("state,touches_ui\nhello,A\n")
    with pytest.raises(ImportRefused, match="line 2: state is not a JSON object"):
        read_import_file(f, "touches_ui", {"A", "B"})


def test_refuses_empty_and_missing_file(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text("state,touches_ui\n")
    with pytest.raises(ImportRefused, match="no rows"):
        read_import_file(f, "touches_ui", {"A"})
    with pytest.raises(ImportRefused, match="cannot read"):
        read_import_file(tmp_path / "nope.csv", "touches_ui", {"A"})


def test_source_counts():
    rows = [
        ExportRow("{}", "A", "job:demo:001"),
        ExportRow("{}", "A", "bench:mine/003-todo-web"),
        ExportRow("{}", "B", "external:synth-v1"),
        ExportRow("{}", "B", "external:synth-v1"),
    ]
    assert source_counts(rows) == Counter({"own": 1, "bench": 1, "external:synth-v1": 2})


def test_imported_refs_are_never_held_out():
    from swfactory.definition.models import BenchSuiteDef

    suite = BenchSuiteDef.model_construct(tasks={})
    assert holdout_overlap("mine", suite, ["external:synth-v1"]) == []


KW = dict(origin_url="https://example.org/s", licence="CC-BY-4.0", imported_by="me@x")


@pytest.mark.db
async def test_import_tags_rows_and_records_dataset(db, tmp_path):  # noqa: F811
    async with db() as s, s.begin():
        d = await import_rows(
            s, DEFN, question="touches_ui", path=csv_with(tmp_path, 3), source="synth-v1", **KW
        )
        assert (d.name, d.rows, d.licence, len(d.sha256)) == ("synth-v1", 3, "CC-BY-4.0", 64)
        assert {r.source for r in await export_rows(s, "touches_ui")} == {"external:synth-v1"}
        assert await export_rows(s, "kind") == []


@pytest.mark.db
async def test_same_sha256_refused_even_under_another_name(db, tmp_path):  # noqa: F811
    f = csv_with(tmp_path, 3)
    async with db() as s, s.begin():
        await import_rows(s, DEFN, question="touches_ui", path=f, source="synth-v1", **KW)
        with pytest.raises(ImportRefused, match="this file is already imported as synth-v1"):
            await import_rows(s, DEFN, question="touches_ui", path=f, source="copy", **KW)
        assert await count_rows(s, "laya_datasets") == 1


@pytest.mark.db
async def test_refused_file_stores_nothing(db, tmp_path):  # noqa: F811
    async with db() as s, s.begin():
        with pytest.raises(ImportRefused):
            await import_rows(
                s, DEFN, question="touches_ui", path=csv_bad_label(tmp_path), source="bad", **KW
            )
        assert await count_rows(s, "laya_datasets") == 0
        assert await count_rows(s, "laya_imported_rows") == 0


@pytest.mark.db
async def test_unknown_question_and_bad_source_name(db, tmp_path):  # noqa: F811
    async with db() as s, s.begin():
        with pytest.raises(ImportRefused, match="unknown question colour"):
            await import_rows(
                s, DEFN, question="colour", path=csv_with(tmp_path, 1), source="a", **KW
            )
        with pytest.raises(ImportRefused, match="source name"):
            await import_rows(
                s, DEFN, question="touches_ui", path=csv_with(tmp_path, 1), source="Bad Name!", **KW
            )
        with pytest.raises(ImportRefused, match="--origin and --licence"):
            await import_rows(
                s, DEFN, question="touches_ui", path=csv_with(tmp_path, 1), source="a",
                origin_url="", licence="l", imported_by="me@x",
            )  # fmt: skip


@pytest.mark.db
async def test_tier_question_labels_come_from_the_definition(db, tmp_path):  # noqa: F811
    f = tmp_path / "t.csv"
    f.write_text('state,tier.builder\n"{}",NOPE\n')
    async with db() as s, s.begin():
        with pytest.raises(ImportRefused, match="line 2: label NOPE is not one of"):
            await import_rows(s, DEFN, question="tier.builder", path=f, source="t", **KW)


@pytest.mark.db
async def test_imported_rows_count_toward_the_fit_minimum(db, tmp_path):  # noqa: F811
    async with db() as s, s.begin():
        await seed_touches_ui(s, 10)
        await import_rows(
            s, DEFN, question="touches_ui", path=csv_with(tmp_path, 1000, (500, 500)),
            source="synth-v1", **KW,
        )  # fmt: skip
        h = await fit_question(s, URL, "k", "touches_ui", transport=FAKE_FIT)
        assert h.rows == 1010
        assert h.source_counts == {"own": 10, "external:synth-v1": 1000} and h.active is False
        assert "external:synth-v1" in h.train_refs


@pytest.mark.db
async def test_too_few_rows_still_refused_with_counts(db, tmp_path):  # noqa: F811
    async with db() as s, s.begin():
        await import_rows(
            s,
            DEFN,
            question="touches_ui",
            path=csv_with(tmp_path, 20, (10, 10)),
            source="tiny",
            **KW,
        )
        with pytest.raises(
            NotEnoughRows, match="20 labelled rows for touches_ui, need at least 50"
        ):
            await fit_question(s, URL, "k", "touches_ui")


@pytest.mark.db
async def test_activation_still_needs_own_evaluation(db, tmp_path):  # noqa: F811
    async with db() as s, s.begin():
        await import_rows(
            s, DEFN, question="touches_ui", path=csv_with(tmp_path, 100, (50, 50)),
            source="synth-v1", **KW,
        )  # fmt: skip
        h = await fit_question(s, URL, "k", "touches_ui", transport=FAKE_FIT)
        with pytest.raises(ActivationRefused, match="no passing evaluation"):
            await activate_head(s, URL, "k", "touches_ui", h.version, by="me@x", transport=FAKE_FIT)


@pytest.mark.db
def test_cli_import_exit_codes(pg_url, monkeypatch, tmp_path):
    args = (
        "laya", "import", "--question", "touches_ui", "--file", str(csv_with(tmp_path, 3)),
        "--source", "synth-v1", "--origin", "https://example.org/s", "--licence", "CC-BY-4.0",
    )  # fmt: skip
    try:
        _cli_env(pg_url, monkeypatch)
        asyncio.run(_clean(pg_url))
        first = run(*args)
        assert first.exit_code == 0, first.output
        assert "imported synth-v1: 3 rows for touches_ui (sha256 " in first.output
        second = run(*args)
        assert second.exit_code == 2 and "already imported as synth-v1" in second.output
        missing = run(
            "laya", "import", "--question", "touches_ui", "--file", "x.csv", "--source", "a"
        )
        assert missing.exit_code == 2
        st = run("laya", "status")
        line = next(ln for ln in st.output.splitlines() if ln.startswith("touches_ui"))
        assert "rows=3 (external:synth-v1: 3)" in line, st.output
        out = tmp_path / "o.csv"
        ex = run("laya", "export", "--question", "touches_ui", "--out", str(out))
        assert ex.exit_code == 0 and "sources: external:synth-v1: 3" in ex.output, ex.output
    finally:
        get_settings.cache_clear()
        asyncio.run(_clean(pg_url))


@pytest.mark.db
async def test_migration_downgrades_to_named_revision(pg_url):
    from alembic import command
    from alembic.config import Config

    from swfactory.db.migrate import alembic_config

    cfg: Config = alembic_config(pg_url)
    engine = make_engine(pg_url)
    try:
        async with engine.connect() as conn:
            await truncate_app_tables(conn)
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.downgrade, cfg, "m11_01_laya_heads")
    try:
        engine = make_engine(pg_url)
        async with engine.connect() as conn:
            n = (await conn.execute(text("select to_regclass('app.laya_datasets')"))).scalar_one()
            assert n is None
        await engine.dispose()
    finally:
        await asyncio.to_thread(command.upgrade, cfg, "head")
