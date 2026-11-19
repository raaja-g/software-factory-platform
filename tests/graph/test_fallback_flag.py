"""Engine model swaps are flagged on the run row and as model.fallback (M6.7)."""

import uuid
from pathlib import Path

import pytest
from tests.support.gateway_env import make_env

from swfactory.domain.types import ModelRef, Role, RunStatus, SelectionMode, ToolPolicy
from swfactory.engines.fake import FakeScript
from swfactory.graph.steps import run_agent_step
from swfactory.models.selection import ModelChoice
from swfactory.sandbox.base import RunnerSpec
from swfactory.sandbox.paths import REPO

pytestmark = pytest.mark.db

FIX = Path(__file__).parents[1] / "fixtures" / "fake"
STD = RunnerSpec("standard", "img", 2.0, 4096, 512, ())
CLASSIFIERS = ["claude-opus-4-8@vertex", "claude-opus-5@vertex", "claude-sonnet-5@vertex"]


@pytest.fixture
def deps_with(sessions, monkeypatch):
    async def make(script, *, engine, model, classifier_fallbacks=None):
        defaults = {"engine_retry_backoff_s": [0, 0]}
        if classifier_fallbacks is not None:
            defaults["claude_classifier_fallbacks"] = classifier_fallbacks
        env = await make_env(sessions, monkeypatch, defaults=defaults)
        fs = (
            FakeScript.from_yaml(FIX / script)
            if isinstance(script, str)
            else FakeScript.from_dict(script)
        )
        env.script.entries = fs.entries
        env.engine, env.model = engine, model
        return env

    return make


async def review_step(env):
    choice = ModelChoice(
        engine=env.engine,
        model=ModelRef(env.model, "vertex", env.model.split("@")[0]),
        mode=SelectionMode.FIXED,
        tier=None,
        decision_id=uuid.uuid4(),
        reason="test",
    )
    async with env.deps.sandboxes.isolated(env.job, STD, name="review-code-r1") as box:
        out = await run_agent_step(
            env.deps,
            job_id=env.job,
            role=Role.REVIEWER_CODE,
            round=1,
            inputs=["diff.patch"],
            output_name="round-1/review-code.md",
            workdir=REPO,
            tool_policy=ToolPolicy.READ_RUN,
            sandbox=box,
            engine=env.engine,
            choice=choice,
        )
    return out, (await env.runs("reviewer-code"))[-1]


async def test_swap_is_flagged_not_silent(deps_with):
    env = await deps_with("swap.yaml", engine="fake", model="gemini-pro@vertex")
    out, run = await review_step(env)
    assert run.fallback_detected is True
    assert run.used_models == ["gemini-3.5-flash-lite"]
    assert run.requested_model == "gemini-pro@vertex"
    ev = await env.events("model.fallback")
    assert {
        "source": "engine",
        "role": "reviewer-code",
        "round": 1,
        "requested": "gemini-pro@vertex",
        "used": ["gemini-3.5-flash-lite"],
    } in ev
    assert out.status is RunStatus.OK


async def test_matching_model_not_flagged(deps_with):
    env = await deps_with(
        {
            "turns": [
                {
                    "role": "reviewer-code",
                    "round": 1,
                    "used_models": ["gemini-pro@vertex"],
                    "output": "VERDICT: PASS\nfine",
                }
            ]
        },
        engine="fake",
        model="gemini-pro@vertex",
    )
    _, run = await review_step(env)
    assert run.fallback_detected is False
    assert await env.events("model.fallback") == []


async def test_classifier_fallback_is_not_a_swap(deps_with):
    env = await deps_with(
        "classifier-fallback.yaml",
        engine="claude_code",
        model="claude-sonnet@vertex",
        classifier_fallbacks=CLASSIFIERS,
    )
    _, run = await review_step(env)
    assert run.fallback_detected is False
    assert run.used_models == ["claude-sonnet@vertex", "claude-opus-5@vertex"]


async def test_classifier_list_ignored_for_other_engines(deps_with):
    env = await deps_with(
        "classifier-fallback.yaml",
        engine="opencode",
        model="claude-sonnet@vertex",
        classifier_fallbacks=["claude-opus-5@vertex"],
    )
    _, run = await review_step(env)
    assert run.fallback_detected is True
