from __future__ import annotations

import functools

import pytest
from sqlalchemy import text

from swfactory.domain.types import Role, TriageMode
from swfactory.graph.steps import pinned_definition, run_agent_step
from swfactory.models.triage import TriageService

pytestmark = pytest.mark.db
ALL_LLM: dict[str, TriageMode] = dict.fromkeys(
    ("kind", "size", "risk", "touches_ui"), TriageMode.LLM
)


async def _run(step, outputs):
    await step.put("feature.md", "# Feature\nFix docs")
    await step.put("app.md", "# App")
    step.script.entries = []
    from swfactory.engines.fake import FakeScript

    turns = [
        {"role": "triage", "round": 1, "attempt": n, "output": o} for n, o in enumerate(outputs, 1)
    ]
    step.script.entries = [*FakeScript.from_dict({"turns": turns}).entries]
    job, _, app = await pinned_definition(step.deps, step.job)
    svc = TriageService(
        step.deps.definitions,
        step.deps.artifacts,
        llm_step=functools.partial(run_agent_step, step.deps),
    )
    return await svc.triage(job, app, modes=ALL_LLM)


async def test_review_focus_1_near_miss_repaired_once(step, sessions):
    r = await _run(
        step,
        [
            "**Kind:** docs\nsize: S\nrisk: low\ntouches_ui: false",
            "kind: docs\nsize: S\nrisk: low\ntouches_ui: false",
        ],
    )
    runs = await step.runs(Role.TRIAGE)
    assert (len(runs), runs[0].error.startswith("contract: "), r.kind, r.source["kind"]) == (
        2,
        True,
        "docs",
        "llm",
    )
    async with sessions.begin() as s:
        await s.execute(text("truncate app.model_decisions cascade"))


async def test_two_near_misses_fall_back_to_rules(step, sessions):
    bad = "kind: chore\nsize: S\nrisk: low\ntouches_ui: false"
    r = await _run(step, [bad, bad])
    assert (r.kind, set(r.source.values())) == ("feature", {"rules"})
    async with sessions() as s:
        reason = await s.scalar(
            text("select reason from app.model_decisions where purpose='triage' limit 1")
        )
    assert reason is not None and reason.startswith("llm fallback: contract: ")
    async with sessions.begin() as s:
        await s.execute(text("truncate app.model_decisions cascade"))
