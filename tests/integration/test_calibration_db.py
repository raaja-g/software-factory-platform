from __future__ import annotations

from decimal import Decimal

import pytest
from tests.fakes.deps import seed_demo
from tests.support.db_helpers import enq

from swfactory.db.repo import calibration_table, put_judge_label, replace_scores
from swfactory.scoring.objective import ScoreRow

pytestmark = pytest.mark.db

SC, RS, RS2 = "merge-readiness", "rubric-1", "rubric-2"
GEM, HAIKU = "gemini-lite@vertex", "claude-haiku@vertex"
JS, JS_HAIKU, JS_NEW = "judge-gem", "judge-haiku", "judge-gem-new"


def row(model: str, sha: str, passed: bool) -> ScoreRow:
    return ScoreRow(
        SC, "A" if passed else "C", Decimal(1 if passed else 0), passed, "r",
        judge_model=model, rubric_sha=RS, judge_sha=sha,
    )  # fmt: skip


async def seed_labels(sessions):
    """30 labelled jobs, 15 human pass (A) and 15 fail (C); gemini disagrees on 2 + 2."""
    await seed_demo(sessions)
    ids = [(await enq(sessions, app="demo")).id for _ in range(30)]
    async with sessions.begin() as s:
        for i, jid in enumerate(ids):
            human_pass = i < 15
            judge_pass = human_pass if i % 15 < 13 else not human_pass
            await put_judge_label(s, jid, SC, "A" if human_pass else "C", RS, "me@x.io")
            rows = [row(GEM, JS, judge_pass)]
            if i < 3:
                rows.append(row(HAIKU, JS_HAIKU, human_pass))
            await replace_scores(s, jid, rows, scorers=[SC])
    return ids


async def test_calibration_per_model_and_rubric(sessions):
    await seed_labels(sessions)
    async with sessions() as s:
        t = await calibration_table(s)
    c = t[(SC, GEM, RS, JS)]
    assert c.kappa is not None
    assert (c.n, round(c.kappa, 3), c.human_pass, c.human_fail, c.calibrated) == (
        30, 0.733, 15, 15, True,
    )  # fmt: skip
    assert t[(SC, HAIKU, RS, JS_HAIKU)].calibrated is False
    assert (SC, GEM, RS2, JS) not in t


async def test_repointed_judge_starts_over(sessions):
    ids = await seed_labels(sessions)
    async with sessions.begin() as s:
        for jid in ids[:2]:
            await replace_scores(s, jid, [row(GEM, JS_NEW, True)], scorers=[SC])
    async with sessions() as s:
        t = await calibration_table(s)
    new = t[(SC, GEM, RS, JS_NEW)]
    assert (new.n, new.calibrated) == (2, False)


async def test_new_label_replaces_old(sessions):
    ids = await seed_labels(sessions)
    async with sessions.begin() as s:
        await put_judge_label(s, ids[0], SC, "C", RS, "me@x.io")
    async with sessions() as s:
        c = (await calibration_table(s))[(SC, GEM, RS, JS)]
    assert (c.n, c.human_pass, c.human_fail) == (30, 14, 16)
