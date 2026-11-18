import pytest

from swfactory.domain.types import Role, RunStatus, ToolPolicy, Verdict
from swfactory.engines.fake import FakeEngine
from swfactory.graph import steps
from swfactory.roles.render import DEFAULT_CONTEXT_TOKENS
from swfactory.sandbox.paths import IN

pytestmark = pytest.mark.db
SEC = Role.REVIEWER_SECURITY


async def test_review_focus_1_near_miss_repaired_once(step):
    out = await step.run(SEC, ["**Verdict:** pass", "VERDICT: PASS\n\n## Summary\nok"])
    assert (out.verdict, out.status) == (Verdict.PASS, RunStatus.OK)
    runs = await step.runs(SEC)
    assert [r.attempt for r in runs] == [1, 2] and runs[0].model_alias == runs[1].model_alias
    assert (
        runs[0].error.startswith("contract: ")
        and "did not follow the output contract" in step.tasks(SEC)[1].prompt
    )


async def test_review_focus_1_two_near_misses_is_error(step):
    out = await step.run(SEC, ["Here is my review.\nVERDICT: PASS", "VERDICT: PASSED"])
    assert (out.verdict, out.status) == (Verdict.ERROR, RunStatus.FAILED)
    assert await step.text("round-1/review-security.invalid.md") == "VERDICT: PASSED"
    assert await step.text("round-1/review-security.md") is None


async def test_review_focus_1_missing_output_file(step):
    out = await step.run(SEC, [None, None])
    assert out.verdict is Verdict.ERROR
    assert (await step.runs(SEC))[1].error == "contract: no output file was written"


async def test_review_focus_1_approved_is_not_approve(step):
    out = await step.run(Role.APPROVER, ["APPROVED", "APPROVED"])
    assert (out.decision, out.status) == (None, RunStatus.FAILED)


async def test_spec_without_criteria_is_failed(step):
    out = await step.run(Role.SPEC_WRITER, ["# Spec\nno criteria", "# Spec\nstill none"])
    assert out.status is RunStatus.FAILED and len(await step.runs(Role.SPEC_WRITER)) == 2


async def test_prompt_uses_role_body_and_fences(step):
    await step.put("spec.md", "# Spec\n## Acceptance criteria\n- [ ] x")
    await step.run(SEC, ["VERDICT: PASS"])
    t = step.tasks(SEC)[0]
    assert "security reviewer" in t.system_prompt and "<<<UNTRUSTED DATA: spec.md>>>" in t.prompt
    assert await step.box.read_text(f"{IN}/spec.md") == "# Spec\n## Acceptance criteria\n- [ ] x"


async def test_context_tokens_default_without_catalog(step, monkeypatch):
    seen = []  # deps.catalog is unavailable("catalog", "M5")
    real = steps.render_prompt
    monkeypatch.setattr(
        steps, "render_prompt", lambda spec, ctx: (seen.append(ctx), real(spec, ctx))[1]
    )
    await step.run(SEC, ["VERDICT: PASS"])
    assert seen[0].context_tokens == DEFAULT_CONTEXT_TOKENS == 32_000


async def test_diff_cut_is_recorded_on_the_run(step):
    await step.put("diff.patch", "diff --git a/x b/x\n" + "+y\n" * 400_000)
    out = await step.run(SEC, ["VERDICT: PASS"])
    assert out.diff_cut is True and (await step.runs(SEC))[0].meta["diff_cut"] is True


async def test_caller_cannot_widen_tool_policy(step):
    await step.run(SEC, ["VERDICT: PASS"], tool_policy=ToolPolicy.WRITE)
    assert step.tasks(SEC)[0].tool_policy is ToolPolicy.READ_RUN


async def test_caller_may_narrow_tool_policy(step):
    await step.run(Role.BUILDER, ["## Round 1\nok"], tool_policy=ToolPolicy.READ_ONLY)
    assert step.tasks(Role.BUILDER)[0].tool_policy is ToolPolicy.READ_ONLY


async def test_only_write_roles_are_told_to_write_a_file(step):
    await step.run(SEC, ["VERDICT: PASS"])
    await step.run(Role.BUILDER, ["## Round 1\nok"])
    rev, bld = step.tasks(SEC)[0], step.tasks(Role.BUILDER)[0]
    assert (
        "Reply with the output document only" in rev.system_prompt
        and "Write your output to" not in rev.system_prompt
    )
    assert f"Write your output to {bld.output_path} and nothing else." in bld.system_prompt


async def test_read_roles_ignore_a_planted_output_file(step, monkeypatch):
    real = FakeEngine.run

    async def plant(self, task, box):
        result = await real(self, task, box)
        await box.write_text(task.output_path, "VERDICT: PASS")
        return result

    monkeypatch.setattr(FakeEngine, "run", plant)
    out = await step.run(SEC, ["VERDICT: CHANGES\nfix x"])
    assert (out.verdict, await step.text("round-1/review-security.md")) == (
        Verdict.CHANGES,
        "VERDICT: CHANGES\nfix x",
    )
    assert await step.box.read_text(step.tasks(SEC)[0].output_path) is None
