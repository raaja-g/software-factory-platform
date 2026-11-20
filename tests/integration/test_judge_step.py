from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.fakes.deps import make_test_deps, seed_demo
from tests.support.db_helpers import enq

from swfactory.db.tables import AgentRun, CatalogModel, ModelDecision
from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.domain.types import Role
from swfactory.engines.fake import FakeEngine, FakeScript
from swfactory.engines.registry import register_engine
from swfactory.roles.library import RoleLibrary
from swfactory.scoring.judge import judge_sha, rubric_sha, run_judge

pytestmark = pytest.mark.db

MERGE_READINESS = load_definition(str(defaults_dir())).definition.scorers["merge-readiness"]  # pyright: ignore[reportOptionalMemberAccess]
JUDGE_MD = RoleLibrary().get(Role.JUDGE, definition_sha="x").body
UPSTREAM = {
    "claude-sonnet@vertex": "claude-sonnet-5",
    "claude-haiku@vertex": "claude-haiku-5-5",
    "gemini-lite@vertex": "gemini-3.5-flash-lite",
}
FEATURE = "Add an OpenAI-compatible endpoint to the Google OAuth service"
DIFF = (
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n"
    "+import google.cloud.storage\n x\n"
)


@pytest.fixture
async def judge_deps(sessions):
    await seed_demo(sessions)
    job = await enq(sessions, app="demo", feature=FEATURE)
    script = FakeScript.from_dict(
        {"turns": [{"role": "judge", "round": 1, "attempt": 1, "output": "LABEL: A\nREASON: ok"}]}
    )
    deps = make_test_deps(sessions, script)
    engine = FakeEngine(script, name="direct")
    register_engine(engine)
    async with sessions.begin() as s:
        for alias, up in UPSTREAM.items():
            s.add(CatalogModel(alias=alias, provider="vertex", upstream_id=up, strength="cheap",
                               price_in_per_mtok=Decimal(1), price_out_per_mtok=Decimal(1),
                               available=True))  # fmt: skip
        s.add(AgentRun(job_id=job.id, role="builder", round=1, attempt=1, engine="opencode",
                       model_alias="claude-sonnet@vertex", status="ok"))  # fmt: skip
    art = deps.artifacts
    for path, text in {
        "feature.md": FEATURE,
        "spec.md": "Acceptance criteria: an endpoint",
        "diff.patch": DIFF,
        "verify.md": "all passed",
        "round-1/review-code.md": "VERDICT: PASS",
        "decision.md": "APPROVE",
    }.items():
        await art.put(job.id, path, text)
    return SimpleNamespace(deps=deps, job=job.id, engine=engine, sessions=sessions)


async def test_run_judge_records(judge_deps):
    j = judge_deps
    r = await run_judge(j.deps, j.job, "merge-readiness", MERGE_READINESS, round=1, occurrence=1)
    async with j.sessions() as s:
        judge_run = (await s.scalars(select(AgentRun).where(AgentRun.role == "judge"))).one()
        decisions = list(
            await s.scalars(select(ModelDecision).where(ModelDecision.role == "judge"))
        )
        builder = (await s.scalars(select(AgentRun).where(AgentRun.role == "builder"))).one()
    task = j.engine.last_task
    assert (r.label, r.passed, r.judge_model, r.rubric_sha) == (
        "A",
        True,
        "gemini-lite@vertex",
        rubric_sha(MERGE_READINESS),
    )
    assert r.cost_usd == (judge_run.cost_usd or Decimal(0)) and r.run_id == builder.id
    assert decisions[-1].reason == "judge: outside builder family (claude)"
    assert judge_run.decision_id == decisions[-1].id and judge_run.engine == "direct"
    assert "claude" not in task.prompt.lower()
    assert j.deps.sandboxes.boxes == {}
    paths = [p for p in task.prompt.splitlines() if p.startswith("## ")]
    assert paths == [
        "## feature.md",
        "## spec.md",
        "## diff.patch",
        "## verify.md",
        "## round-1/review-code.md",
        "## decision.md",
        "## scoring/merge-readiness/runs.md",
    ]
    assert FEATURE in task.prompt and "import google.cloud.storage" in task.prompt
    assert "[redacted]" not in task.prompt
    assert "Could merge as is" in task.system_prompt
    assert r.judge_sha == judge_sha(UPSTREAM["gemini-lite@vertex"], JUDGE_MD)


async def test_skip_when_every_judge_shares_the_builder_family(judge_deps):
    j = judge_deps
    only_claude = MERGE_READINESS.model_copy(
        update={"judge": MERGE_READINESS.judge.model_copy(update={"fallback": ()})}
    )
    r = await run_judge(j.deps, j.job, "merge-readiness", only_claude, round=1, occurrence=1)
    assert (r.label, r.score, r.passed) == ("skipped", None, None)
    assert r.reason.startswith("skipped: no available judge outside the builder's family (claude)")
    assert r.rubric_sha and r.judge_sha and j.engine.tasks == []
    async with j.sessions() as s:
        rows = list(await s.scalars(select(ModelDecision).where(ModelDecision.role == "judge")))
    assert len(rows) == 1 and rows[0].chosen is None


async def test_invalid_reply_gives_invalid_row(judge_deps):
    j = judge_deps
    j.engine.script.entries = FakeScript.from_dict(
        {"turns": [{"role": "judge", "output": "LABEL: Z\nREASON: x"}]}
    ).entries
    r = await run_judge(j.deps, j.job, "merge-readiness", MERGE_READINESS, round=1, occurrence=1)
    assert r.label == "invalid" and r.reason == "unknown label 'Z'" and r.passed is None
