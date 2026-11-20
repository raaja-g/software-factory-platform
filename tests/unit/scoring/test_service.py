from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.graph.test_loop import ctx, events, run_job

from swfactory.db.tables import AgentRun, Score
from swfactory.scoring import service as svc_mod
from swfactory.scoring.objective import ScoreRow
from swfactory.scoring.service import ScoringService, quality_badge


def row(scorer, label, passed=True):
    return ScoreRow(scorer, label, Decimal(1) if passed else Decimal(0), passed, "r")


CLEAN = [
    row("obj.delivery", "merged_unchanged"),
    row("obj.decision", "APPROVE"),
    row("obj.human", "none"),
    row("obj.fallback", "none"),
]


def with_(rows, **kw):
    over = {f"obj.{k}": v for k, v in kw.items()}
    seen = {r.scorer for r in rows}
    out = [row(r.scorer, over[r.scorer]) if r.scorer in over else r for r in rows]
    return out + [row(k, v) for k, v in over.items() if k not in seen]


def test_quality_good():
    assert quality_badge(CLEAN).label == "good"
    assert quality_badge(CLEAN).scorer == "obj.quality"


def test_quality_fair_on_rework():
    assert quality_badge(with_(CLEAN, human="rework")).label == "fair"


def test_quality_fair_on_fallback_or_changed_head():
    assert quality_badge(with_(CLEAN, fallback="1 of 5 runs")).label == "fair"
    assert quality_badge(with_(CLEAN, delivery="merged_changed")).label == "fair"


def test_quality_poor_when_closed_or_reverted():
    closed = quality_badge(with_(CLEAN, delivery="closed")).label
    reverted = quality_badge(with_(CLEAN, reverted="reverted")).label
    assert closed == reverted == "poor"


def test_judges_do_not_change_badge():
    judge = ScoreRow("merge-readiness", "F", Decimal(0), False, "bad")
    assert quality_badge([*CLEAN, judge]).label == "good"


pytestmark_db = pytest.mark.db


@pytest.fixture
def bound(sessions):
    ctx.sessions = sessions
    return sessions


class FakeJudge:
    def __init__(self, sessions, boom=False):
        self.calls: list[tuple[str, int]] = []
        self.sessions = sessions
        self.boom = boom

    async def __call__(self, deps, job_id, name, scorer, *, round, occurrence, **kw):
        self.calls.append((name, occurrence))
        if self.boom:
            raise RuntimeError("boom")
        async with self.sessions.begin() as s:
            s.add(AgentRun(job_id=job_id, role="judge", round=round, attempt=occurrence,
                           engine="fake", status="ok"))  # fmt: skip
        return ScoreRow(name, "B", Decimal("0.7"), True, "ok", judge_model="gemini-lite@vertex",
                        cost_usd=Decimal("0.0041"))  # fmt: skip


@pytest.fixture
async def scored(bound, monkeypatch):
    job, _ = await run_job("happy.yaml")
    d = ctx.deps
    monkeypatch.setattr(
        svc_mod, "sampled", lambda jid, name, rate, benchmark=False: name == "merge-readiness"
    )
    svc = ScoringService(bound, d.events, d.definitions, d.catalog)
    return SimpleNamespace(job=job, deps=d, svc=svc, sessions=bound)


async def names(sessions, job_id):
    async with sessions() as s:
        return {r.scorer for r in await s.scalars(select(Score).where(Score.job_id == job_id))}


@pytest.mark.db
async def test_score_job_order_and_event(scored, monkeypatch):
    fj = FakeJudge(scored.sessions)
    monkeypatch.setattr(svc_mod, "run_judge", fj)
    s = await scored.svc.score_job(scored.deps, scored.job.id)
    assert fj.calls == [("merge-readiness", 1)]
    assert s.judges_skipped == (
        "process-compliance: not sampled",
        "scope-creep: not sampled",
        "spec-adherence: not sampled",
    )
    assert s.quality == "good" and s.judges_run == 1 and s.errors == ()
    ev = (await events(scored.job.id, "score.recorded"))[-1]
    assert ev.data == {"quality": "good", "judges": 1, "errors": 0}
    assert {"obj.quality", "merge-readiness", "obj.gate"} <= await names(
        scored.sessions, scored.job.id
    )


@pytest.mark.db
async def test_score_job_never_raises(scored, monkeypatch):
    monkeypatch.setattr(svc_mod, "run_judge", FakeJudge(scored.sessions, boom=True))
    s = await scored.svc.score_job(scored.deps, scored.job.id)
    assert s.errors == ("judge merge-readiness: boom",)
    assert {"obj.gate", "obj.decision", "obj.delivery"} <= await names(
        scored.sessions, scored.job.id
    )


@pytest.mark.db
async def test_force_runs_all_with_next_occurrence(scored, monkeypatch):
    fj = FakeJudge(scored.sessions)
    monkeypatch.setattr(svc_mod, "run_judge", fj)
    await scored.svc.score_job(scored.deps, scored.job.id)
    await scored.svc.score_job(scored.deps, scored.job.id, force=True)
    assert ("merge-readiness", 2) in fj.calls
    assert {n for n, _ in fj.calls} == {
        "merge-readiness",
        "process-compliance",
        "scope-creep",
        "spec-adherence",
    }


@pytest.mark.db
async def test_only_limits_judges(scored, monkeypatch):
    fj = FakeJudge(scored.sessions)
    monkeypatch.setattr(svc_mod, "run_judge", fj)
    await scored.svc.score_job(scored.deps, scored.job.id, only=["scope-creep"])
    assert [n for n, _ in fj.calls] == ["scope-creep"]


@pytest.mark.db
async def test_missing_job_never_raises(scored):
    from uuid import uuid4

    s = await scored.svc.score_job(scored.deps, uuid4())
    assert s.errors and s.quality is None


async def _noop():
    await asyncio.sleep(0)
