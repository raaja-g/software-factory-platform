from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.db.repo import load_job_facts, replace_scores
from swfactory.db.tables import Artifact, Score
from swfactory.scoring.objective import OBJ_SCORERS, ScoreRow, compute_signals

pytestmark = pytest.mark.db

ROWS = [
    ScoreRow("obj.gate", "passed", Decimal("1.0"), True, "ok"),
    ScoreRow("obj.rounds", "1", Decimal(1), None, "r"),
    ScoreRow("obj.cost_usd", "$1.00", Decimal(1), None, "c", rubric_sha="x", judge_sha="y"),
]


async def count_scores(s, job_id):
    return await s.scalar(select(func.count()).select_from(Score).where(Score.job_id == job_id))


async def test_replace_scores_is_idempotent(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    async with sessions.begin() as s:
        await replace_scores(s, job.id, ROWS, scorers=OBJ_SCORERS)
        await replace_scores(s, job.id, ROWS, scorers=OBJ_SCORERS)
    async with sessions() as s:
        assert await count_scores(s, job.id) == len(ROWS)


async def test_null_score_and_passed_are_stored(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    row = ScoreRow("obj.delivery", "delivered", None, None, "waiting")
    async with sessions.begin() as s:
        await replace_scores(s, job.id, [row], scorers=["obj.delivery"])
    async with sessions() as s:
        got = (await s.scalars(select(Score).where(Score.job_id == job.id))).one()
        assert (got.score, got.passed) == (None, None)


async def test_load_job_facts_reads_gate_artifacts(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo")
    body = {"status": "passed", "reason": None, "exit_code": 0, "protected": [], "screenshots": []}
    async with sessions.begin() as s:
        for rnd, att in ((1, 2), (1, 1)):
            s.add(
                Artifact(
                    job_id=job.id,
                    path=f"verify/r{rnd}-a{att}.json",
                    kind="json",
                    content=json.dumps(body),
                )
            )
    async with sessions() as s:
        f = await load_job_facts(s, job.id)
    assert [g["path"] for g in f.gates] == ["verify/r1-a1.json", "verify/r1-a2.json"]
    rows = {r.scorer: r for r in compute_signals(f)}
    assert rows["obj.fix_attempts"].score == 1
