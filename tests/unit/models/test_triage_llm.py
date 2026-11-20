from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.db.tables import ModelDecision
from swfactory.definition.store import DefinitionStore
from swfactory.domain.types import Role, RunStatus, ToolPolicy, TriageMode
from swfactory.graph.deps import Deps  # noqa: F401  (keeps import order stable)
from swfactory.graph.steps import StepOutcome
from swfactory.models.triage import TRIAGE_QUESTIONS, TriageService, merge_triage

QUESTIONS = tuple(TRIAGE_QUESTIONS)
DEFAULTS = {"kind": "feature", "size": "M", "risk": "low", "touches_ui": False}
LLM = TriageMode.LLM


def test_llm_merge_rules_last():
    v, src, why = merge_triage(
        {"risk": "high"},
        DEFAULTS,
        {},
        dict.fromkeys(QUESTIONS, LLM),
        llm_values={"kind": "docs", "size": "S", "risk": "low", "touches_ui": True},
    )
    assert (v["kind"], v["risk"], src["kind"], why["kind"], why["risk"]) == (
        "docs",
        "high",
        "llm",
        "llm: answer used",
        "rule override",
    )
    assert v["touches_ui"] is True and src["risk"] == "rules"


def test_llm_merge_fallback_reason():
    v, src, why = merge_triage({}, DEFAULTS, {}, dict.fromkeys(QUESTIONS, LLM), llm_error="boom")
    assert v == DEFAULTS and set(src.values()) == {"rules"}
    assert why["kind"] == "llm fallback: boom"


class Step:
    def __init__(self, outcome: StepOutcome | Exception):
        self.outcome = outcome
        self.calls: list[dict] = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def ok(body: str) -> StepOutcome:
    return StepOutcome(uuid.uuid4(), RunStatus.OK, body, None, None, False)


def failed(body: str | None) -> StepOutcome:
    return StepOutcome(uuid.uuid4(), RunStatus.FAILED, body, None, None, False)


ALL_LLM = dict.fromkeys(QUESTIONS, LLM)


@pytest.fixture
async def ctx(sessions):
    async def clean():
        async with sessions.begin() as s:
            await s.execute(text("truncate app.model_decisions cascade"))

    await clean()
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    row = SimpleNamespace(store=DefinitionStore(sessions))
    import tempfile
    from pathlib import Path

    from swfactory.artifacts import ArtifactStore

    arts = ArtifactStore(sessions, Path(tempfile.mkdtemp(prefix="art-")))
    async with sessions() as s:
        from swfactory.db.tables import Job

        j = await s.get(Job, job.id)
    assert j is not None
    _, _, app = await _pinned(sessions, j)
    row.job, row.app, row.arts = j, app, arts

    def svc(step):
        return TriageService(row.store, row.arts, llm_step=step)

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


async def _pinned(sessions, job):
    from swfactory.definition.models import AppDef

    store = DefinitionStore(sessions)
    defn = await store.get(job.definition_sha)
    assert defn is not None
    app: AppDef = next(iter(defn.factory.apps.values()))
    return job, defn, app


@pytest.mark.db
async def test_llm_answer_used(ctx):
    step = Step(ok("kind: docs\nsize: S\nrisk: low\ntouches_ui: false"))
    r = await ctx.svc(step).triage(ctx.job, ctx.app, modes=ALL_LLM)
    assert (r.kind, r.size, r.source["kind"]) == ("docs", "S", "llm")
    kw = step.calls[0]
    assert (
        kw.items()
        >= {
            "role": Role.TRIAGE,
            "tool_policy": ToolPolicy.NONE,
            "output_name": "triage-llm.md",
        }.items()
    )
    rows = await ctx.rows()
    assert [(d.purpose, d.mode, d.reason) for d in rows] == [
        ("triage", "llm", "llm: answer used")
    ] * 4
    assert rows[0].router_version is None and rows[0].answer["question"] == "kind"
    assert "| kind | docs | llm |" in await ctx.arts.get_text(ctx.job.id, "triage.md")


@pytest.mark.db
async def test_invalid_answer_falls_back(ctx):
    step = Step(failed("kind: chore\nsize: S\nrisk: low\ntouches_ui: false"))
    r = await ctx.svc(step).triage(ctx.job, ctx.app, modes=ALL_LLM)
    assert set(r.source.values()) == {"rules"} and r.kind == "feature"
    rows = await ctx.rows()
    assert rows[0].reason.startswith("llm fallback: contract: unknown kind 'chore'")


@pytest.mark.db
async def test_step_exception_falls_back(ctx):
    r = await ctx.svc(Step(RuntimeError("no model for triage"))).triage(
        ctx.job, ctx.app, modes=ALL_LLM
    )
    assert set(r.source.values()) == {"rules"}
    assert (await ctx.rows())[0].reason == "llm fallback: no model for triage"


@pytest.mark.db
async def test_no_llm_step(ctx):
    r = await ctx.svc(None).triage(ctx.job, ctx.app, modes=ALL_LLM)
    assert r.source["kind"] == "rules"
    assert (await ctx.rows())[0].reason == "llm fallback: no triage model"


@pytest.mark.db
async def test_rules_mode_makes_no_step_call(ctx):
    step = Step(ok("x"))
    await ctx.svc(step).triage(ctx.job, ctx.app)
    assert step.calls == [] and await ctx.rows() == []
