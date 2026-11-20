from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.artifacts import ArtifactStore
from swfactory.db.tables import Job, ModelDecision
from swfactory.definition.models import TriageConfig
from swfactory.definition.store import DefinitionStore
from swfactory.domain.types import TriageMode
from swfactory.models.router_client import LayaAnswer
from swfactory.models.triage import (
    TOUCHES_UI_LABELS,
    TRIAGE_QUESTIONS,
    TriageService,
    merge_triage,
    triage_state,
)
from swfactory.roles.render import estimate_tokens

DEFAULTS = {"kind": "feature", "size": "M", "risk": "low", "touches_ui": False}
LAYA, SHADOW = TriageMode.LAYA, TriageMode.SHADOW
QS = ("kind", "size", "risk", "touches_ui")
ALL_SHADOW: dict[str, TriageMode] = dict.fromkeys(QS, SHADOW)
ALL_LAYA: dict[str, TriageMode] = dict.fromkeys(QS, LAYA)


def ans(choice, route, error=None):
    return LayaAnswer(choice, {}, route, 5, error, "h1", "laya 0.4.31")


def all_err(e):
    return {q: LayaAnswer(None, {}, None, 5, e) for q in QS}


def fake_job(**kw):
    base = {"title": "t", "feature": "f", "triage": {"labels": ["x"]}}
    return SimpleNamespace(**{**base, **kw})


APP = SimpleNamespace(repo_url="https://example.com/o/demo.git", ui_globs=("**/*.html",))


def test_questions_follow_r05():
    assert {q: v["type"] for q, v in TRIAGE_QUESTIONS.items()} == dict.fromkeys(QS, "choice")
    assert list(TRIAGE_QUESTIONS["kind"]["criteria"]) == [
        "feature",
        "bugfix",
        "refactor",
        "docs",
        "infra",
    ]
    assert list(TRIAGE_QUESTIONS["size"]["criteria"]) == ["S", "M", "L"]
    assert list(TRIAGE_QUESTIONS["touches_ui"]["criteria"]) == ["A", "B"]
    assert TOUCHES_UI_LABELS == {"A": True, "B": False}


def test_state_under_512_tokens():
    st = triage_state(fake_job(feature="x" * 50_000), APP)
    assert estimate_tokens(json.dumps(st)) < 512
    assert st["feature"].endswith(" [omitted]")
    assert set(st) == {"title", "feature", "app", "stack", "labels", "ui_globs"}


def test_every_criterion_described():
    assert all(d.strip() for q in TRIAGE_QUESTIONS.values() for d in q["criteria"].values())


def test_modes_and_rules_last():
    v, src, why = merge_triage(
        {"risk": "high"},
        DEFAULTS,
        {
            "kind": ans("bugfix", "auto"),
            "size": ans("L", "review"),
            "risk": ans("low", "auto"),
            "touches_ui": ans("A", "auto"),
        },
        {"kind": LAYA, "size": LAYA, "risk": LAYA, "touches_ui": SHADOW},
    )
    assert v == {"kind": "bugfix", "size": "M", "risk": "high", "touches_ui": False}
    assert src == {"kind": "laya", "size": "rules", "risk": "rules", "touches_ui": "rules"}
    assert why["size"] == "route review: rules decided"
    assert why["risk"] == "rule override" and why["touches_ui"] == "shadow: rules decided"


def test_laya_touches_ui_label_maps_to_bool():
    v, _, _ = merge_triage({}, DEFAULTS, {"touches_ui": ans("A", "auto")}, {"touches_ui": LAYA})
    assert v["touches_ui"] is True


def test_router_error_uses_rules():
    why = merge_triage({}, DEFAULTS, all_err("timeout"), ALL_LAYA)[2]
    assert why["kind"] == "laya fallback: router error (timeout)"


def test_config_modes_field():
    c = TriageConfig(modes={"kind": LAYA})
    assert dict(c.modes) == {"kind": LAYA} and TriageConfig().modes == {}


class Spy:
    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers

    async def decide(self, state, questions, *, pins=None):
        self.calls.append(SimpleNamespace(state=state, questions=questions, pins=pins))
        if self.answers is not None:
            return self.answers
        return {q: ans(next(iter(c["criteria"])), "auto") for q, c in questions.items()}


@pytest.fixture
async def ctx(sessions):
    async def clean():
        async with sessions.begin() as s:
            await s.execute(text("truncate app.model_decisions cascade"))

    await clean()
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    async with sessions() as s:
        j = await s.get(Job, job.id)
    assert j is not None
    store = DefinitionStore(sessions)
    defn = await store.get(j.definition_sha)
    assert defn is not None
    app = next(iter(defn.factory.apps.values()))
    arts = ArtifactStore(sessions, Path(tempfile.mkdtemp(prefix="art-")))
    row = SimpleNamespace(job=j, app=app, store=store, arts=arts)

    def svc(router, config_modes=None):
        if config_modes is not None:
            real = store.get

            async def get(sha):
                d = await real(sha)
                assert d is not None
                tri = d.factory.triage.model_copy(update={"modes": config_modes})
                return d.model_copy(
                    update={"factory": d.factory.model_copy(update={"triage": tri})}
                )

            fake_store = SimpleNamespace(get=get, sessions=store.sessions)
            return TriageService(fake_store, arts, router=router)  # type: ignore[arg-type]
        return TriageService(store, arts, router=router)

    async def rows():
        async with sessions() as s:
            return list(
                (
                    await s.scalars(
                        select(ModelDecision)
                        .where(ModelDecision.job_id == j.id, ModelDecision.purpose == "triage")
                        .order_by(ModelDecision.created_at, ModelDecision.id)
                    )
                ).all()
            )

    row.svc, row.rows = svc, rows
    yield row
    await clean()


@pytest.mark.db
async def test_one_batch_call_and_four_rows(ctx):
    spy = Spy()
    await ctx.svc(spy, ALL_SHADOW).triage(ctx.job, ctx.app)
    assert len(spy.calls) == 1 and len(spy.calls[0].questions) == 4
    rows = await ctx.rows()
    assert [(r.purpose, r.role) for r in rows] == [("triage", "triage")] * 4
    assert rows[0].router_version == "laya 0.4.31" and rows[0].head_version == "h1"
    assert {r.answer["question"] for r in rows} == set(QS)
    assert rows[0].answer["state"] == spy.calls[0].state
    assert {r.mode for r in rows} == {"shadow"}


@pytest.mark.db
async def test_laya_mode_uses_answer_and_writes_source(ctx):
    spy = Spy(
        {
            "kind": ans("docs", "auto"),
            "size": ans("S", "auto"),
            "risk": ans("low", "review"),
            "touches_ui": ans("A", "auto"),
        }
    )
    r = await ctx.svc(spy).triage(ctx.job, ctx.app, modes=ALL_LAYA)
    assert (r.kind, r.size, r.touches_ui) == ("docs", "S", True)
    assert r.source["kind"] == "laya" and r.source["risk"] == "rules"
    md = await ctx.arts.get_text(ctx.job.id, "triage.md")
    assert "| kind | docs | laya |" in md


@pytest.mark.db
async def test_rules_mode_never_calls_router(ctx):
    spy = Spy()
    await ctx.svc(spy, {}).triage(ctx.job, ctx.app)
    assert spy.calls == [] and await ctx.rows() == []


@pytest.mark.db
async def test_pins_reach_router(ctx):
    spy = Spy()
    await ctx.svc(spy, ALL_SHADOW).triage(ctx.job, ctx.app, pins={"kind": 2})
    assert spy.calls[0].pins == {"kind": 2}


@pytest.mark.db
async def test_call_modes_replace_config(ctx):
    spy = Spy()
    await ctx.svc(spy, ALL_SHADOW).triage(ctx.job, ctx.app, modes={"kind": LAYA})
    got = {r.answer["question"]: r.mode for r in await ctx.rows()}
    assert got == {"kind": "laya", "size": "shadow", "risk": "shadow", "touches_ui": "shadow"}


@pytest.mark.db
async def test_no_router_falls_back_to_rules(ctx):
    r = await ctx.svc(None).triage(ctx.job, ctx.app, modes=ALL_LAYA)
    assert (r.kind, r.size) == ("feature", "M") and r.source["kind"] == "rules"
