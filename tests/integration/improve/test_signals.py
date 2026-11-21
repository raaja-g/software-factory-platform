from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from swfactory.db.tables import AgentRun, App, Command, Improvement, Job, JudgeLabel, Score
from swfactory.domain.types import Role
from swfactory.improve.signals import collect_signals, failures_since, last_improvement_at

pytestmark = pytest.mark.db

T0 = datetime(2026, 11, 1, tzinfo=UTC)


async def _app(s) -> App:
    a = App(slug="demo", repo_kind="local", repo_url="/tmp/x")
    s.add(a)
    await s.flush()
    return a


async def _job(s, app, seq, *, kind="normal", at=T0, **kw) -> Job:
    j = Job(
        app_id=app.id,
        seq=seq,
        slug=f"j{seq}",
        title="t",
        feature="f",
        kind=kind,
        requested_by="me",
        branch=f"b{seq}",
        budget_usd=Decimal("1"),
        definition_sha="x",
        profile_name="p",
        finished_at=at,
        **kw,
    )
    s.add(j)
    await s.flush()
    return j


async def _run(s, job, role, *, verdict=None, rnd=0, status="ok", error=None) -> AgentRun:
    r = AgentRun(
        job_id=job.id,
        role=role,
        round=rnd,
        engine="e",
        status=status,
        verdict=verdict,
        error=error,
    )
    s.add(r)
    await s.flush()
    return r


async def _clean(sessions):
    async with sessions.begin() as s:
        await s.execute(text("truncate table app.apps restart identity cascade"))
        await s.execute(text("truncate table app.improvements restart identity cascade"))


@pytest.fixture
async def clean(sessions):
    await _clean(sessions)
    yield sessions
    await _clean(sessions)


async def test_attribution(clean):
    sessions = clean
    async with sessions.begin() as s:
        app = await _app(s)
        j1 = await _job(s, app, 1, stage="failed", gate_reason="step failed: verify")
        j2 = await _job(s, app, 2, stage="needs_human", decision="ESCALATE")
        await _run(s, j2, "reviewer-security", verdict="CHANGES", rnd=1)
        await _run(s, j2, "reviewer-code", verdict="PASS", rnd=1)
        await _run(s, j2, "reviewer-ux", verdict="CHANGES", rnd=0)
        j3 = await _job(s, app, 3)
        await _run(s, j3, "reviewer-security", verdict="ERROR", error="boom")
        j4 = await _job(s, app, 4, stage="needs_human", decision="ESCALATE")
        s.add(Command(job_id=j4.id, action="approve", requested_by="me", consumed_at=T0))
        j5 = await _job(s, app, 5)
        s.add(
            Command(job_id=j5.id, action="rework", note="fix it", requested_by="me", consumed_at=T0)
        )
        j6 = await _job(s, app, 6)
        run = await _run(s, j6, "spec-writer")
        s.add(Score(job_id=j6.id, run_id=run.id, scorer="obj.gate", label="failed", passed=False))
        del j1
    async with sessions() as s:
        sigs = await collect_signals(s, since=None)
    by_role: dict[Role, set[str]] = {}
    for x in sigs:
        by_role.setdefault(x.role, set()).add(x.kind)
    assert by_role[Role.BUILDER] == {"failed", "rework"}
    assert by_role[Role.REVIEWER_SECURITY] == {"escalated", "reviewer_error"}
    assert Role.REVIEWER_UX not in by_role and Role.REVIEWER_CODE not in by_role
    assert by_role[Role.APPROVER] == {"override", "escalated"}
    assert by_role[Role.SPEC_WRITER] == {"score"}


async def test_judge_score_needs_calibration(clean):
    sessions = clean
    async with sessions.begin() as s:
        app = await _app(s)
        for i in range(30):
            j = await _job(s, app, i + 1)
            run = await _run(s, j, "spec-writer")
            ok = i % 3 != 0
            label = "pass" if ok else "fail"
            s.add(
                Score(
                    job_id=j.id,
                    run_id=run.id,
                    scorer="spec-adherence",
                    label=label,
                    passed=ok,
                    judge_model="m",
                    rubric_sha="r",
                    judge_sha="s",
                )
            )
            s.add(
                JudgeLabel(
                    job_id=j.id,
                    scorer="spec-adherence",
                    label=label,
                    rubric_sha="r",
                    labelled_by="me",
                )
            )
    async with sessions() as s:
        sigs = await collect_signals(s, since=None)
    assert {x.kind for x in sigs if x.role == Role.SPEC_WRITER} == {"score"}
    assert len([x for x in sigs if x.role == Role.SPEC_WRITER]) == 10


async def test_since_last_improvement(clean):
    sessions = clean
    async with sessions.begin() as s:
        app = await _app(s)
        for i in range(3):
            await _job(s, app, i + 1, at=T0, stage="failed", gate_reason="step failed: build")
        for i in range(2):
            await _job(s, app, i + 10, at=T0 + timedelta(days=2), stage="failed", gate_reason="x")
        imp = Improvement(trigger="manual", role_group="builder", base_sha="a" * 40, suite="v")
        s.add(imp)
        await s.flush()
        await s.execute(
            text("update app.improvements set created_at = :t"), {"t": T0 + timedelta(days=1)}
        )
    async with sessions() as s:
        last = await last_improvement_at(s)
        n, oldest = await failures_since(s, last)
        assert n == 2 and oldest == T0 + timedelta(days=2)
        assert (await failures_since(s, None))[0] == 5


async def test_benchmark_and_improver_jobs_ignored(clean):
    sessions = clean
    async with sessions.begin() as s:
        app = await _app(s)
        await _job(s, app, 1, stage="failed")
        await _job(s, app, 2, kind="benchmark", stage="failed")
        j = await _job(s, app, 3, kind="improver", stage="failed")
        j.app_id = None
    async with sessions() as s:
        assert (await failures_since(s, None))[0] == 1
        assert len(await collect_signals(s, since=None)) <= 1


async def test_uncalibrated_judge_failure_gives_no_signal(clean):
    sessions = clean
    async with sessions.begin() as s:
        app = await _app(s)
        for i in range(3):
            j = await _job(s, app, i + 1)
            run = await _run(s, j, "spec-writer")
            s.add(
                Score(
                    job_id=j.id,
                    run_id=run.id,
                    scorer="spec-adherence",
                    label="fail",
                    passed=False,
                    judge_model="m",
                    rubric_sha="r",
                    judge_sha="s",
                )
            )
            s.add(
                JudgeLabel(
                    job_id=j.id,
                    scorer="spec-adherence",
                    label="fail",
                    rubric_sha="r",
                    labelled_by="me",
                )
            )
    async with sessions() as s:
        got = await collect_signals(s, since=None)
    assert [x for x in got if x.kind == "score" and x.role == Role.SPEC_WRITER] == []
