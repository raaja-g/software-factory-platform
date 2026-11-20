"""M11.9: fixed-vs-laya evaluation, the switch verdict and the activation gate."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update
from tests.support.pg import truncate_app_tables
from tests.unit.cli.conftest import run
from tests.unit.laya.test_export import db  # noqa: F401  (fixture)

from swfactory.bench.report import BenchReport, ConfigSummary
from swfactory.db.engine import make_engine, make_sessionmaker
from swfactory.db.tables import BenchResult, BenchRun, LayaHead
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import BenchConfigDef, BenchSuiteDef, BenchTaskDef, Definition
from swfactory.domain.types import Role, SelectionMode, TriageMode
from swfactory.laya import evaluate as ev
from swfactory.laya.evaluate import (
    ActivationRefused,
    EvaluationRefused,
    activate_head,
    evaluation_configs,
    holdout_overlap,
    run_evaluation,
    switch_verdict,
)
from swfactory.settings import get_settings

SHA = "a" * 40
URL = "http://router:8000"


def summary(
    name="x", *, hidden=0.75, judge: float | None = 1.0, cost="2.00", trials=4, completed=4
):
    return ConfigSummary(
        name=name, trials=trials, completed=completed, not_run=0, hidden_pass_rate=hidden,
        judge_pass_rate=judge, judge_pass_rate_uncalibrated=None, quality=hidden,
        mean_rounds=1.0, total_cost_usd=Decimal(cost) * trials, mean_cost_usd=Decimal(cost),
        median_duration_s=10.0,
    )  # fmt: skip


def report(*, fixed, laya):
    from dataclasses import replace

    return BenchReport(
        run_id=uuid4(), suite="holdout", definition_sha="d", status="done",
        budget_usd=Decimal(10), cost_usd=Decimal(1),
        configs=(replace(fixed, name="fixed"), replace(laya, name="laya")),
        chart=(), pareto=(), recommended=None, recommendation_reason="", lowest_cost=None,
        best_quality=None,
    )  # fmt: skip


FIXED = summary(hidden=0.75, judge=1.0, cost="2.00")


def _task(source_job=None):
    return BenchTaskDef(
        title="t", feature="f", repo="r", base_sha=SHA, merge_sha=SHA, definition_sha=SHA,
        check_command="pytest {file}", source_job=source_job,
    )  # fmt: skip


V1 = BenchSuiteDef(
    configurations=(BenchConfigDef(name="a", profile="team-gemini"),),
    tasks={
        "001-todo-api": _task("v1:001"),
        "002-todo-cli": _task("v1:002"),
        "003-todo-web": _task(),
    },
)


def test_switch_when_quality_holds_and_cheaper():
    v = switch_verdict(
        report(fixed=FIXED, laya=summary(hidden=0.75, judge=1.0, cost="1.25")),
        question="tier.builder",
        head_version="tier.builder@v1",
    )
    assert v.switch and v.reasons == () and isinstance(v.laya, ConfigSummary)


def test_null_judge_on_both_sides_is_equal():
    v = switch_verdict(
        report(fixed=summary(judge=None), laya=summary(judge=None, cost="1")),
        question="q",
        head_version="h",
    )
    assert v.switch


def test_holdout_overlap():
    refs = ["bench:v1-parity/003-todo-web", "job:v1:002", "job:demo:007", "external:x/1"]
    assert holdout_overlap("v1-parity", V1, refs) == ["002-todo-cli", "003-todo-web"]
    assert holdout_overlap("other", V1, ["external:v1-parity/003-todo-web"]) == []


def test_evaluate_needs_two_repetitions():
    r = run("laya", "evaluate", "--question", "tier.builder", "--suite", "holdout",
            "--repetitions", "1")  # fmt: skip
    assert r.exit_code == 2 and "at least 2 repetitions" in r.output


@pytest.mark.parametrize(
    "laya,head,why",
    [
        (summary(hidden=0.50, cost="1"), "h", "hidden-check pass rate fell 0.75 -> 0.50"),
        (summary(judge=0.75, cost="1"), "h", "judge pass rate fell 1.00 -> 0.75"),
        (summary(cost="1.85"), "h", "not 10% cheaper: $1.85 vs $2.00"),
        (summary(trials=4, completed=3, cost="1"), "h", "laya finished 3 of 4 tasks"),
        (summary(cost="1"), None, "no fitted head for tier.builder"),
    ],
)
def test_no_switch(laya, head, why):
    v = switch_verdict(report(fixed=FIXED, laya=laya), question="tier.builder", head_version=head)
    assert why in v.reasons and not v.switch


def test_tier_configs_validate_as_bench_config_def():
    f, la = evaluation_configs("team-gemini", "tier.builder", version=3)
    assert f == BenchConfigDef.model_validate(
        {"name": "fixed", "profile": "team-gemini", "roles": {"builder": {"mode": "fixed"}}}
    )
    assert la == BenchConfigDef.model_validate(
        {
            "name": "laya", "profile": "team-gemini",
            "roles": {"builder": {"mode": "laya"}}, "laya_heads": {"tier.builder": 3},
        }
    )  # fmt: skip
    assert (la.roles[Role.BUILDER].mode, la.roles[Role.BUILDER].model, la.triage_modes) == (
        SelectionMode.LAYA, None, {},
    )  # fmt: skip


def test_triage_configs_validate_as_bench_config_def():
    f, la = evaluation_configs("team-gemini", "kind", version=1)
    assert f == BenchConfigDef.model_validate(
        {"name": "fixed", "profile": "team-gemini", "triage_modes": {"kind": "rules"}}
    )
    assert la == BenchConfigDef.model_validate(
        {
            "name": "laya", "profile": "team-gemini",
            "triage_modes": {"kind": "laya"}, "laya_heads": {"kind": 1},
        }
    )  # fmt: skip
    assert (la.triage_modes["kind"], la.roles) == (TriageMode.LAYA, {})
    assert BenchConfigDef.model_validate(la.model_dump(mode="json")) == la


def test_unknown_question():
    pytest.raises(ValueError, evaluation_configs, "team-gemini", "colour", version=1)
    pytest.raises(ValueError, evaluation_configs, "team-gemini", "tier.judge", version=1)


def test_laya_heads_validation():
    with pytest.raises(ValueError, match="unknown question"):
        BenchConfigDef(name="a", profile="p", laya_heads={"colour": 1})
    with pytest.raises(ValueError):
        BenchConfigDef(name="a", profile="p", laya_heads={"kind": 0})


# --- database ---------------------------------------------------------------------------


def _fake_router(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert request.url.path == "/v1/factory/heads/activate"
    assert request.headers["authorization"] == "Bearer k"
    return httpx.Response(200, json={"active": f"{body['question_id']}@v{body['version']}"})


FAKE_ROUTER = httpx.MockTransport(_fake_router)


def _head(q, v, evaluation=None, active=False):
    return LayaHead(
        question=q, version=v, rows=60, label_counts={}, questions_sha256="q", csv_sha256="c",
        path=f"/heads/{q}/v{v}", head_sha256="h" * 64, active=active, train_refs=[],
        evaluation=evaluation,
    )  # fmt: skip


@pytest.mark.db
async def test_activate_gate_and_rollback(db):  # noqa: F811
    async with db() as s, s.begin():
        s.add_all([
            _head("tier.builder", 1, {"switch": True}, active=True),
            _head("tier.builder", 2, {"switch": False, "reasons": ["x"]}),
            _head("tier.builder", 3, {"switch": True}),
            _head("kind", 1, {"switch": True}, active=True),
        ])  # fmt: skip

    async def active(q):
        async with db() as s:
            rows = await s.scalars(
                select(LayaHead.version).where(LayaHead.question == q, LayaHead.active.is_(True))
            )
            return list(rows)

    async with db() as s, s.begin():
        with pytest.raises(ActivationRefused, match="tier.builder v2 has no passing evaluation"):
            await activate_head(s, URL, "k", "tier.builder", 2, by="a@x", transport=FAKE_ROUTER)
    assert await active("tier.builder") == [1]
    async with db() as s, s.begin():
        h = await activate_head(s, URL, "k", "tier.builder", 3, by="a@x", transport=FAKE_ROUTER)
        assert (h.activated_by, h.activated_at is not None) == ("a@x", True)
    assert await active("tier.builder") == [3] and await active("kind") == [1]
    async with db() as s, s.begin():
        await activate_head(s, URL, "k", "tier.builder", 1, by="a@x", transport=FAKE_ROUTER)
    assert await active("tier.builder") == [1]


@pytest.mark.db
async def test_activation_keeps_active_head_when_router_refuses(db):  # noqa: F811
    async with db() as s, s.begin():
        s.add_all(
            [_head("kind", 1, {"switch": True}, active=True), _head("kind", 2, {"switch": True})]
        )
    no = httpx.MockTransport(lambda r: httpx.Response(422, json={"detail": "bad hash"}))
    async with db() as s, s.begin():
        with pytest.raises(Exception, match="http 422"):
            await activate_head(s, URL, "k", "kind", 2, by="a", transport=no)
    async with db() as s:
        rows = await s.scalars(select(LayaHead.version).where(LayaHead.active.is_(True)))
        assert list(rows) == [1]


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


@pytest.mark.db
def test_cli_activate_is_refused_without_a_passing_evaluation(pg_url, monkeypatch):
    async def seed(s):
        s.add(_head("kind", 1, {"switch": False}))

    try:
        _cli_env(pg_url, monkeypatch)
        asyncio.run(_with_db(pg_url, seed))
        r = run("laya", "activate", "--question", "kind", "--version", "1")
        assert r.exit_code == 2 and "kind v1 has no passing evaluation" in r.output
    finally:
        get_settings.cache_clear()
        asyncio.run(_with_db(pg_url, _noop))


async def _noop(s):
    return None


def _defn(tmp_path: Path, tasks: dict[str, BenchTaskDef]) -> Definition:
    init_definition_repo(tmp_path)
    r = load_definition(str(tmp_path))
    assert r.definition is not None
    suite = BenchSuiteDef(
        configurations=(BenchConfigDef(name="a", profile="team-gemini"),), tasks=tasks
    )
    return r.definition.model_copy(update={"benchmarks": {"holdout": suite}})


@pytest.fixture
def active_defn(monkeypatch, tmp_path):
    box: dict[str, Definition] = {}

    async def load_active(s):
        return "sha1", box["d"]

    monkeypatch.setattr(ev, "load_active", load_active)
    return lambda tasks: box.setdefault("d", _defn(tmp_path, tasks))


async def _yes(est, names):
    return True


@pytest.mark.db
async def test_evaluation_refusals(db, active_defn):  # noqa: F811
    active_defn({"001-a": _task(), "002-b": _task("v1:002")})
    kw = dict(suite="holdout", version=None, repetitions=2, profile=None,
              requested_by="a", confirm=_yes, poll_s=0.01)  # fmt: skip
    with pytest.raises(EvaluationRefused, match="no fitted head for kind"):
        await run_evaluation(db, question="kind", **kw)  # type: ignore[arg-type]
    with pytest.raises(EvaluationRefused, match="unknown question 'colour'"):
        await run_evaluation(db, question="colour", **kw)  # type: ignore[arg-type]
    with pytest.raises(EvaluationRefused, match="at least 2 repetitions"):
        await run_evaluation(db, question="kind", **{**kw, "repetitions": 1})  # type: ignore[arg-type]
    async with db() as s, s.begin():
        s.add(_head("kind", 3, None))
        await s.execute(update(LayaHead).values(train_refs=["job:v1:002", "bench:holdout/001-a"]))
    with pytest.raises(
        EvaluationRefused,
        match=r"holdout tasks 001-a, 002-b are in kind v3's training rows; evaluate on a holdout",
    ):
        await run_evaluation(db, question="kind", **kw)  # type: ignore[arg-type]
    async with db() as s:
        assert (await s.scalars(select(BenchRun))).all() == []


@pytest.mark.db
async def test_evaluation_runs_both_configs_and_records_the_verdict(db, active_defn, tmp_path):  # noqa: F811
    active_defn({"001-a": _task(), "002-b": _task()})
    async with db() as s, s.begin():
        s.add(_head("kind", 1, None))

    async def finish():
        while True:
            await asyncio.sleep(0.02)
            async with db() as s, s.begin():
                runs = (await s.scalars(select(BenchRun))).all()
                if not runs:
                    continue
                for r in (await s.scalars(select(BenchResult))).all():
                    cost = "2.00" if r.configuration == "fixed" else "1.00"
                    r.status, r.hidden_passed, r.judge_pass = "done", True, True
                    r.rounds, r.cost_usd = 1, Decimal(cost)
                runs[0].status = "done"
                return runs[0].id

    seen: list = []

    async def confirm(est, names):
        seen.append((est.trials, names))
        return True

    task = asyncio.create_task(finish())
    out = await run_evaluation(
        db, question="kind", suite="holdout", version=None, repetitions=2, profile=None,
        requested_by="a", confirm=confirm, out_dir=tmp_path / "var", poll_s=0.02,
    )  # fmt: skip
    run_id = await task
    assert out is not None
    verdict, rep, path = out
    assert seen == [(8, ["fixed", "laya"])]
    assert verdict.switch and path.name == f"eval-kind-{str(run_id)[:8]}.md"
    assert "switch: yes" in path.read_text()
    async with db() as s:
        results = (await s.scalars(select(BenchResult))).all()
        head = await s.scalar(select(LayaHead))
    laya_rows = [r for r in results if r.configuration == "laya"]
    assert len(results) == 8 and {r.repetition for r in results} == {1, 2}
    assert {json.dumps(r.overrides["laya_heads"]) for r in laya_rows} == {'{"kind": 1}'}
    assert {r.overrides["triage_modes"]["kind"] for r in laya_rows} == {"laya"}
    assert all(r.overrides["laya_heads"] == {} for r in results if r.configuration == "fixed")
    assert head is not None and head.evaluation["switch"] is True
    assert head.evaluation["run_id"] == str(run_id) and head.evaluation["suite"] == "holdout"
    assert set(head.evaluation) == {"run_id", "suite", "repetitions", "switch", "reasons", "at"}


@pytest.mark.db
async def test_only_benchmark_jobs_pin_heads(db):  # noqa: F811
    from types import SimpleNamespace

    from tests.unit.laya.test_export import make_app, make_job

    from swfactory.graph.steps import benchmark_laya_pins

    async with db() as s, s.begin():
        a = await make_app(s)
        bench, normal = await make_job(s, a, 1), await make_job(s, a, 2)
        bench.kind = "benchmark"
        r = BenchRun(suite="holdout", configurations=[])
        s.add(r)
        await s.flush()
        ov = {"roles": {}, "triage_modes": {}, "laya_heads": {"kind": 2}}
        s.add(
            BenchResult(run_id=r.id, task="t", configuration="laya", job_id=bench.id, overrides=ov)
        )
    deps = SimpleNamespace(sessions=db)
    assert await benchmark_laya_pins(deps, bench) == {"kind": 2}  # type: ignore[arg-type]
    assert await benchmark_laya_pins(deps, normal) == {}  # type: ignore[arg-type]
