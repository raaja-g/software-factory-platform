import pytest
from tests.support.m3_loop import happy, v1_feature, v1_replay

from swfactory.domain.types import Role
from swfactory.sandbox.paths import IN

pytestmark = pytest.mark.db


async def test_v1_job_003_replay(loop):
    r = await loop.run(v1_replay("003-todo-web"), feature=v1_feature("003-todo-web"))
    assert (r.job.stage, r.state["round"], r.state["decision"]) == ("merged", 2, "APPROVE")
    assert r.ok_runs() == {
        "spec-writer": 1,
        "builder": 2,
        "reviewer-security": 2,
        "reviewer-ux": 2,
        "reviewer-ui": 2,
        "reviewer-code": 2,
        "approver": 1,
    }
    assert {"feature.md", "app.md", "triage.md", "round-1/review-security.md"} <= set(r.artifacts)
    p = r.tasks(Role.SPEC_WRITER)[0].prompt
    assert "<<<UNTRUSTED DATA: feature.md>>>" in p and "<<<UNTRUSTED DATA: app.md>>>" in p


async def test_prepare_puts_feature_and_app_md(loop):
    r = await loop.run(happy())
    assert r.artifacts["feature.md"] == "# Say hello\n\nAdd a hello command\n"
    assert {"slug: demo", "test command: uv run pytest"} <= set(r.artifacts["app.md"].splitlines())
    assert await r.box.read_text(f"{IN}/app.md") == r.artifacts["app.md"]
    assert await r.box.read_text(f"{IN}/feature.md") == r.artifacts["feature.md"]


async def test_ui_rule_forces_ui_reviewer(loop):
    r = await loop.run(
        happy(), app={"reviewers": ["security", "code"]}, diff_paths=["web/index.html"]
    )
    assert (
        r.state["reviewers"] == ["security", "ui", "code"]
        and len(r.runs_for(Role.REVIEWER_UI)) == 1
    )


async def test_relevance_skip_records_skipped(loop):
    r = await loop.run(happy(), app={"reviewer_relevance": True}, diff_paths=["src/a.py"])
    assert (r.state["reviews"]["1"]["ux"]["verdict"], r.state["reviews"]["1"]["ui"]["verdict"]) == (
        "SKIPPED",
        "SKIPPED",
    )
    assert (
        r.runs_for(Role.REVIEWER_UX),
        r.runs_for(Role.REVIEWER_UI),
        len(r.runs_for(Role.APPROVER)),
    ) == ([], [], 1)


async def test_reviewer_near_miss_goes_to_needs_human(loop):
    r = await loop.run(
        happy({("reviewer-security", 1): ["**Verdict:** pass", "**Verdict:** pass"]})
    )
    assert (r.job.stage, r.state["gate_reason"]) == ("needs_human", "reviewer failed")
    assert r.state["reviews"]["1"]["security"]["verdict"] == "ERROR"


async def test_high_risk_requires_human(loop):
    r = await loop.run(
        happy(),
        triage_rules=[
            {"name": "risky", "match": {}, "set": {"risk": "high"}, "require_human_approval": True}
        ],
    )
    assert (r.job.stage, r.state["decision"], r.state["gate_reason"]) == (
        "needs_human",
        "APPROVE",
        "human approval required (triage)",
    )


async def test_cut_diff_sends_an_approved_job_to_a_person(
    loop,
):  # diff.patch far larger than the 32k-token context
    r = await loop.run(happy(), artifacts={"diff.patch": "diff --git a/x b/x\n" + "+y\n" * 400_000})
    assert (r.job.stage, r.state["decision"], r.state["gate_reason"]) == (
        "needs_human",
        "APPROVE",
        "diff too large to review fully",
    )
    assert r.state["reviews"]["1"]["code"]["diff_cut"] is True and r.state["diff_cut"] is True
