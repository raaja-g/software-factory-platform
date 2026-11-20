from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.db.repo import metrics_rows, put_judge_label, replace_scores
from swfactory.db.tables import AgentRun, Job, JobEvent
from swfactory.scoring.metrics import summarize_metrics
from swfactory.scoring.objective import ScoreRow

pytestmark = pytest.mark.db

T0 = datetime.now(UTC) - timedelta(days=1)
SC, RS, JS, GEM = "merge-readiness", "r1", "j1", "gem@v"


def judge(passed: bool) -> ScoreRow:
    return ScoreRow(
        SC, "A" if passed else "C", Decimal(int(passed)), passed, "r",
        judge_model=GEM, rubric_sha=RS, judge_sha=JS,
    )  # fmt: skip


async def finish(sessions, jid, stage, cost, rounds=1, kind="normal", when=None):
    async with sessions.begin() as s:
        await s.execute(
            update(Job)
            .where(Job.id == jid)
            .values(
                stage=stage,
                cost_usd=Decimal(cost),
                round=rounds,
                kind=kind,
                profile_name="fast",
                finished_at=when or datetime.now(UTC),
            )
        )


async def test_metrics_sql_matches_fixture(sessions):
    await seed_demo(sessions, apps=("todo", "shop"))
    a, b, c, bench, old, other = [(await enq(sessions, app="todo")).id for _ in range(5)] + [
        (await enq(sessions, app="shop")).id
    ]
    await finish(sessions, a, "merged", "2", 1)
    await finish(sessions, b, "merged", "3", 2)
    await finish(sessions, c, "closed", "1", 1)
    await finish(sessions, bench, "closed", "9", 1, kind="benchmark")
    await finish(sessions, old, "merged", "7", 1, when=T0 - timedelta(days=30))
    await finish(sessions, other, "merged", "5", 1)
    async with sessions.begin() as s:
        s.add(JobEvent(job_id=c, kind="human.waiting", data={}))
        run_a = AgentRun(job_id=a, role="builder", engine="opencode", model_alias="opus")
        run_b = AgentRun(
            job_id=b, role="builder", engine="opencode", model_alias="opus", fallback_detected=True
        )
        s.add_all([run_a, run_b])
        await s.flush()
        await replace_scores(
            s, a, [ScoreRow("obj.gate", "passed", None, True, "", run_id=run_a.id), judge(True)],
            scorers=["obj.gate", SC],
        )  # fmt: skip
        await replace_scores(
            s, b, [ScoreRow("obj.gate", "failed", None, False, "", run_id=run_b.id), judge(False)],
            scorers=["obj.gate", SC],
        )  # fmt: skip
        await put_judge_label(s, a, SC, "A", RS, "me")
    async with sessions() as s:
        got = summarize_metrics(await metrics_rows(s, app_slug="todo", since=T0), window_days=30)
        both = summarize_metrics(await metrics_rows(s, app_slug=None, since=T0), window_days=30)
        hidden = await metrics_rows(s, app_slug=None, since=T0, app_slugs=frozenset({"shop"}))
    assert got["jobs"] == 3 and both["jobs"] == 4 and len(hidden.jobs) == 1
    assert got["cost_per_merged_usd"] == "3.000000"
    assert got["escalation_rate"] == pytest.approx(1 / 3)
    assert got["fallback_rate"] == 0.5
    assert got["rounds"] == {
        "mean": pytest.approx(4 / 3),
        "histogram": [{"rounds": 1, "jobs": 2}, {"rounds": 2, "jobs": 1}],
    }
    assert got["pass_rate"]["role"] == [{"key": "builder", "n": 2, "pass_rate": 0.5}]
    assert got["pass_rate"]["engine"] == [{"key": "opencode", "n": 2, "pass_rate": 0.5}]
    assert got["pass_rate"]["profile"][0]["key"] == "fast"
    assert got["scorers"] == [
        {
            "scorer": SC,
            "judge_model": GEM,
            "n": 2,
            "pass_rate": 0.5,
            "calibrated": False,
            "labels": 1,
            "kappa": None,
        }  # fmt: skip
    ]
