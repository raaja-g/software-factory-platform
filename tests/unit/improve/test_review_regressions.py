"""Regression tests for the adversarial review of the improver (each repro failed before its fix).

The proposal is written by an agent that has `bash` in its sandbox, so these tests drive the same
pieces the graph does against a real sandbox directory and a real host clone: seed the sandbox,
let the "agent" edit (and commit) in it, read the changes back, then run the host-side checks."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from tests.fakes.stub_sandbox import StubSandbox

from swfactory.definition.loader import defaults_dir, init_definition_repo, load_definition
from swfactory.definition.models import Definition
from swfactory.domain.types import FactoryMode, Role
from swfactory.improve.proposal import (
    FileChange,
    ProposalCheck,
    check_minimums,
    check_paths,
    check_redraft,
    check_scope,
    describe_model_changes,
    validate_proposal,
)
from swfactory.improve.workspace import (
    DefinitionWorkspace,
    read_changes,
    rebuild_changes,
    seed_sandbox,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "a",
    "GIT_AUTHOR_EMAIL": "a@a",
    "GIT_COMMITTER_NAME": "a",
    "GIT_COMMITTER_EMAIL": "a@a",
}


@dataclass
class Outcome:
    changes: list[FileChange]
    early: list[tuple[str, str, str]]
    violations: list[tuple[str, str, str]]
    check: ProposalCheck | None

    @property
    def rejected(self) -> bool:
        return bool(self.early or self.violations)

    @property
    def messages(self) -> str:
        return " | ".join(m for _, _, m in [*self.early, *self.violations])


class Env:
    """A host definition repo, a host clone at its base, and a sandbox the agent works in."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.src = tmp / "def"
        self.base_sha = init_definition_repo(self.src)
        r = load_definition(str(self.src))
        assert not r.errors and r.definition is not None
        self.base: Definition = r.definition
        self.box = StubSandbox(tmp / "box", None)
        self.ws = DefinitionWorkspace(
            str(self.src), tmp / "ws", job_id=uuid4(), auth=None, gitops=None
        )
        self.repo = self.box.root / "work" / "repo"
        self.files: dict[str, bytes] = {}

    async def start(self) -> None:
        self.files = await self.ws.checkout(self.base_sha)
        await seed_sandbox(self.box, self.files)

    def agent(self, cmd: str) -> None:
        r = subprocess.run(
            ["bash", "-c", cmd],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env={**os.environ, **_GIT_ENV},
        )
        assert r.returncode == 0, r.stderr

    def commit(self) -> None:  # the agent moves the sandbox's HEAD
        self.agent("git -c user.name=a -c user.email=a@a commit -qam forged")

    def edit(self, path: str, old: str, new: str) -> None:
        p = self.repo / path
        t = p.read_text()
        assert old in t, (path, old)
        p.write_text(t.replace(old, new, 1))

    async def pipeline(self, role: Role) -> Outcome:
        """What the graph does between the agent's last edit and the validated commit."""
        changes = await read_changes(self.box, self.files)
        files = await self.ws.checkout(self.base_sha)
        changes = rebuild_changes(changes, files)
        early = [*check_paths(changes), *check_redraft(changes), *check_scope(changes, role)]
        if early:
            return Outcome(changes, _t(early), [], None)
        await self.ws.commit(
            "improve/20261111-" + role.value,
            changes,
            message="m",
            when=datetime(2026, 11, 11, tzinfo=UTC),
        )
        chk = validate_proposal(
            self.ws.path, self.base, changes, mode=FactoryMode.PERSONAL, role=role
        )
        return Outcome(changes, [], _t(chk.violations), chk)


def _t(vs: Any) -> list[tuple[str, str, str]]:
    return [(v.rule, v.path, v.message) for v in vs]


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory(prefix="rev-") as d:
        e = Env(Path(d))
        await e.start()
        yield e


# --------------------------------------------------------------------------
# 1. the "old" side is never read from the sandbox
# --------------------------------------------------------------------------

TEAM = "profiles/team-gemini.yaml"
SEC_LINE = "reviewer-code:     { engine: opencode, model: gemini-flash@vertex }"


async def test_honest_out_of_scope_edit_is_caught(env: Env) -> None:
    env.edit(TEAM, SEC_LINE, SEC_LINE.replace("gemini-flash", "claude-sonnet"))
    out = await env.pipeline(Role.BUILDER)
    assert any("roles.reviewer-code" in m for _, _, m in out.early)


async def test_forged_head_cannot_hide_a_profile_edit(env: Env) -> None:
    env.edit(TEAM, SEC_LINE, SEC_LINE.replace("gemini-flash", "claude-sonnet"))
    env.commit()  # the forbidden edit is now "old"
    env.edit(TEAM, "fallback: [gemini-flash@vertex]", "fallback: [gemini-lite@vertex]")
    env.edit("roles/builder.md", "You are", "You are (redrafted)")
    out = await env.pipeline(Role.BUILDER)
    assert out.rejected
    assert any("roles.reviewer-code" in m for _, _, m in out.early)
    # the host commit never happened, so the real base was not overwritten
    assert (
        "claude-sonnet"
        not in yaml.safe_load((env.src / TEAM).read_text())["roles"]["reviewer-code"]["model"]
    )


async def test_forged_head_cannot_hide_a_catalog_remap_or_budget_raise(env: Env) -> None:
    env.edit(
        "factory.yaml",
        "upstream_id: claude-haiku-5-5,      strength: cheap",
        "upstream_id: gemini-3.8-flash,     strength: cheap",
    )
    env.edit(
        "factory.yaml",
        "upstream_id: claude-sonnet-5-5,     strength: strong",
        "upstream_id: gemini-4-argon,     strength: strong",
    )
    env.edit("factory.yaml", "  job_usd: 10\n", "  job_usd: 10\n  job_max_usd: 9999\n")
    env.commit()
    env.edit("factory.yaml", "job_usd: 10\n", "job_usd: 9\n")  # the only visible diff
    env.edit(
        "roles/reviewer-security.md",
        "You are the factory's **security reviewer**",
        "You are the factory's security reviewer",
    )
    out = await env.pipeline(Role.REVIEWER_SECURITY)
    assert out.rejected
    assert "upstream_id" in out.messages or "models" in out.messages
    assert "job_max_usd" in out.messages


async def test_changes_are_the_difference_from_the_host_base(env: Env) -> None:
    env.edit("roles/builder.md", "You are", "You are (redrafted)")
    env.commit()
    env.agent("rm scorers/scope-creep.md; echo x > scorers/new.md")
    changes = await read_changes(env.box, env.files)
    got = {(c.path, c.status) for c in changes}
    assert got == {
        ("roles/builder.md", "M"),
        ("scorers/scope-creep.md", "D"),
        ("scorers/new.md", "A"),
    }
    by = {c.path: c for c in changes}
    assert by["roles/builder.md"].old_text == env.files["roles/builder.md"].decode()


async def test_rebuild_ignores_stored_old_text_and_status(env: Env) -> None:
    forged = FileChange("roles/builder.md", "A", None, "100644", "forged old", "new\n")
    [fixed] = rebuild_changes([forged], env.files)
    assert fixed.status == "M"
    assert fixed.old_text == env.files["roles/builder.md"].decode()
    gone = FileChange("scorers/x.md", "M", None, "100644", "forged", "n\n")
    [added] = rebuild_changes([gone], env.files)
    assert (added.status, added.old_text) == ("A", None)
    [deleted] = rebuild_changes(
        [FileChange("roles/builder.md", "D", None, "000000", "forged", None)], env.files
    )
    assert deleted.status == "D" and deleted.old_text == env.files["roles/builder.md"].decode()


async def test_rename_is_rebuilt_as_delete_plus_add(env: Env) -> None:
    ren = FileChange("roles/x.md", "R", "roles/builder.md", "100644", "forged", "n\n")
    got = {(c.path, c.status) for c in rebuild_changes([ren], env.files)}
    assert got == {("roles/builder.md", "D"), ("roles/x.md", "A")}


# --------------------------------------------------------------------------
# 2. D17 is evaluated against the base
# --------------------------------------------------------------------------


async def test_moving_the_builder_into_the_reviewers_family_is_rejected(env: Env) -> None:
    env.edit(
        TEAM,
        "    model: gemini-pro@vertex\n    mode: fixed",
        "    model: claude-sonnet@vertex\n    mode: fixed",
    )
    out = await env.pipeline(Role.BUILDER)
    assert out.rejected and "D17" in out.messages


def _team(defn: Definition, **builder: Any) -> Definition:
    prof = defn.profiles["team-gemini"]
    rp = prof.roles[Role.BUILDER].model_copy(update=builder)
    newp = prof.model_copy(update={"roles": {**prof.roles, Role.BUILDER: rp}})
    return defn.model_copy(update={"profiles": {**defn.profiles, "team-gemini": newp}})


_SHIPPED_RESULT = load_definition(str(defaults_dir()))
assert _SHIPPED_RESULT.definition is not None, _SHIPPED_RESULT.errors
SHIPPED: Definition = _SHIPPED_RESULT.definition
assert SHIPPED is not None


def test_builder_fallback_into_the_judges_family_is_rejected() -> None:
    d = _team(SHIPPED, fallback=("claude-haiku@vertex",))
    assert any("D17" in v.message for v in check_minimums(SHIPPED, d))


def test_builder_leaving_a_family_is_fine() -> None:
    prof = SHIPPED.profiles["team-gemini"]
    rp = prof.roles[Role.BUILDER].model_copy(
        update={"model": "gemini-pro@vertex", "fallback": (), "tiers": ()}
    )
    d = SHIPPED.model_copy(
        update={
            "profiles": {
                **SHIPPED.profiles,
                "team-gemini": prof.model_copy(update={"roles": {**prof.roles, Role.BUILDER: rp}}),
            }
        }
    )
    assert check_minimums(SHIPPED, d) == []
    assert check_minimums(SHIPPED, SHIPPED) == []


def test_moving_a_scorer_judge_pair_into_a_new_builder_family_is_rejected() -> None:
    # only team-gemini exists: no builder is on Claude, so the claude judges of the scorers do
    # not collide in base. The builder gains a claude fallback and every judge now shares it.
    only = SHIPPED.model_copy(update={"profiles": {"team-gemini": SHIPPED.profiles["team-gemini"]}})
    d = _team(only, fallback=("gemini-flash@vertex", "claude-opus@vertex"))
    msgs = " ".join(v.message for v in check_minimums(only, d))
    assert "D17" in msgs and "scorers." in msgs


# --------------------------------------------------------------------------
# 3. strength is compared position by position
# --------------------------------------------------------------------------


def test_primary_downgrade_is_not_masked_by_a_weak_fallback() -> None:
    # base: reviewer-security primary claude-sonnet (strong) with a cheap fallback already
    base = SHIPPED
    prof = base.profiles["team-gemini"]
    weak = prof.roles[Role.REVIEWER_SECURITY].model_copy(
        update={"fallback": ("gemini-lite@vertex",)}
    )
    base = base.model_copy(
        update={
            "profiles": {
                **base.profiles,
                "team-gemini": prof.model_copy(
                    update={"roles": {**prof.roles, Role.REVIEWER_SECURITY: weak}}
                ),
            }
        }
    )
    prof = base.profiles["team-gemini"]
    down = prof.roles[Role.REVIEWER_SECURITY].model_copy(update={"model": "gemini-flash@vertex"})
    proposed = base.model_copy(
        update={
            "profiles": {
                **base.profiles,
                "team-gemini": prof.model_copy(
                    update={"roles": {**prof.roles, Role.REVIEWER_SECURITY: down}}
                ),
            }
        }
    )
    msgs = [v.message for v in check_minimums(base, proposed) if "strength" in v.message]
    assert msgs, "primary claude-sonnet (strong) -> gemini-flash (mid) must be a strength drop"


def test_unchanged_positions_are_fine() -> None:
    assert check_minimums(SHIPPED, SHIPPED) == []


# --------------------------------------------------------------------------
# 4. budgets: only never-raise, never-zero
# --------------------------------------------------------------------------

F0 = (defaults_dir() / "factory.yaml").read_text()


def _fy(old: str, new: str) -> FileChange:
    assert old in F0, old
    return FileChange("factory.yaml", "M", None, "100644", F0, F0.replace(old, new, 1))


@pytest.mark.parametrize(
    "old,new",
    [
        ("  job_usd: 10\n", "  job_usd: 10\n  job_max_usd: 9999\n"),
        ("  job_usd: 10\n", "  job_usd: 10\n  step_max_usd: {builder: 999}\n"),
        ("  job_usd: 10\n", "  job_usd: 10\n  app_monthly_usd: {demo: 99999}\n"),
    ],
)
def test_only_the_named_budget_keys_may_change(old: str, new: str) -> None:
    assert check_paths([_fy(old, new)])


@pytest.mark.parametrize(
    "old,new",
    [
        ("  job_usd: 10\n", "  job_usd: 0.0001\n"),
        ("  job_usd: 10\n", "  job_usd: 4\n"),
        ("  daily_usd: 150\n", "  daily_usd: 0\n"),
        ("  daily_usd: 150\n", "  daily_usd: 10\n"),
        ("  job_usd: 10\n", "  job_usd: 11\n"),
    ],
)
def test_budget_floor_and_cap(old: str, new: str, tmp_path: Path) -> None:
    changed = _fy(old, new).new_text
    assert changed is not None
    d = _definition_with_factory(tmp_path, changed)
    assert [v for v in check_minimums(SHIPPED, d) if v.rule == "minimum"]


def test_halving_a_budget_is_allowed(tmp_path: Path) -> None:
    changed = _fy("  job_usd: 10\n", "  job_usd: 5\n").new_text
    assert changed is not None
    d = _definition_with_factory(tmp_path, changed)
    assert check_minimums(SHIPPED, d) == []


def test_budget_floor_in_check_minimums_objects() -> None:
    def with_budgets(**kw: Any) -> Definition:
        b = SHIPPED.factory.budgets.model_copy(update=kw)
        return SHIPPED.model_copy(
            update={"factory": SHIPPED.factory.model_copy(update={"budgets": b})}
        )

    assert check_minimums(SHIPPED, with_budgets(job_usd=Decimal("0.0001")))
    assert check_minimums(SHIPPED, with_budgets(daily_usd=Decimal("0")))
    assert check_minimums(SHIPPED, with_budgets(job_max_usd=Decimal("9999")))
    assert check_minimums(SHIPPED, with_budgets(step_max_usd={"builder": Decimal("999")}))
    assert check_minimums(SHIPPED, with_budgets(job_usd=Decimal("5"))) == []


def test_new_app_budget_must_stay_under_the_job_cap_and_over_half() -> None:
    from tests.unit.improve.test_guardrails import BASE, with_app_budget

    assert check_minimums(BASE, with_app_budget(BASE, "pay", Decimal("11")))
    assert check_minimums(BASE, with_app_budget(BASE, "pay", Decimal("1")))
    assert check_minimums(BASE, with_app_budget(BASE, "pay", Decimal("5"))) == []


def _definition_with_factory(tmp: Path, factory_yaml: str) -> Definition:
    p = tmp / "def"
    init_definition_repo(p)
    (p / "factory.yaml").write_text(factory_yaml)
    r = load_definition(str(p))
    assert r.definition is not None, r.errors
    return r.definition


# --------------------------------------------------------------------------
# 5. scorers: body only, authorised by the OLD applies_to
# --------------------------------------------------------------------------


async def test_scorer_threshold_and_sampling_are_locked(env: Env) -> None:
    env.edit("scorers/spec-adherence.md", "passing_score: 1.0", "passing_score: 0.5")
    env.edit("scorers/spec-adherence.md", "sampling_rate: 0.10", "sampling_rate: 0")
    out = await env.pipeline(Role.BUILDER)
    assert out.rejected


def _scorer(name: str) -> str:
    return (defaults_dir() / "scorers" / f"{name}.md").read_text()


@pytest.mark.parametrize(
    "old,new",
    [
        ("passing_score: 1.0", "passing_score: 0.0"),
        ("sampling_rate: 0.10", "sampling_rate: 0.0"),
        (
            "judge: { model: claude-haiku@vertex, fallback: [gemini-lite@vertex] }",
            "judge: { model: gemini-lite@vertex, fallback: [] }",
        ),
        ("score: 0.5", "score: 1.0"),
        ("applies_to: [builder]", "applies_to: [builder, reviewer-code]"),
    ],
)
def test_scorer_frontmatter_is_locked(old: str, new: str) -> None:
    s = _scorer("process-compliance")
    assert old in s, old
    c = FileChange("scorers/process-compliance.md", "M", None, "100644", s, s.replace(old, new, 1))
    v = check_scope([c], Role.BUILDER)
    assert v and v[0].rule == "scope"


def test_scorer_body_edit_by_the_owning_role_is_fine() -> None:
    s = _scorer("process-compliance")
    c = FileChange("scorers/process-compliance.md", "M", None, "100644", s, s + "\nMore.\n")
    assert check_scope([c], Role.BUILDER) == []


def test_adding_your_role_to_applies_to_does_not_authorise_the_edit() -> None:
    s = _scorer("process-compliance")
    new = s.replace("applies_to: [builder]", "applies_to: [builder, reviewer-code]")
    c = FileChange("scorers/process-compliance.md", "M", None, "100644", s, new + "\nx\n")
    v = check_scope([c], Role.REVIEWER_CODE)
    assert v  # the OLD scope does not include reviewer-code


def test_deleting_or_renaming_a_foreign_scorer_is_out_of_scope() -> None:
    s = _scorer("process-compliance")
    d = FileChange("scorers/process-compliance.md", "D", None, "000000", s, None)
    assert check_scope([d], Role.REVIEWER_CODE)
    r = FileChange("scorers/pc2.md", "R", "scorers/process-compliance.md", "100644", s, s)
    assert check_scope([r], Role.REVIEWER_CODE)


def test_an_added_scorer_is_judged_by_its_own_scope() -> None:
    s = _scorer("process-compliance").replace(
        "applies_to: [builder]", "applies_to: [reviewer-code]"
    )
    a = FileChange("scorers/new-one.md", "A", None, "100644", None, s)
    assert check_scope([a], Role.REVIEWER_CODE) == []
    assert check_scope([a], Role.BUILDER)


def test_a_scorer_used_by_the_benchmark_cannot_be_edited(tmp_path: Path) -> None:
    p = tmp_path / "def"
    init_definition_repo(p)
    f = p / "scorers" / "spec-adherence.md"
    f.write_text(f.read_text() + "\nBe lenient.\n")
    r = load_definition(str(p))
    assert r.definition is not None
    v = check_minimums(SHIPPED, r.definition)
    assert any("spec-adherence" in x.message and "benchmark" in x.message for x in v)
    g = p / "scorers" / "scope-creep.md"  # not referenced by any benchmark task
    f.write_text(_scorer("spec-adherence"))
    g.write_text(g.read_text() + "\nMore.\n")
    r2 = load_definition(str(p))
    assert r2.definition is not None
    assert check_minimums(SHIPPED, r2.definition) == []


# --------------------------------------------------------------------------
# 9. role frontmatter is locked
# --------------------------------------------------------------------------


async def test_role_inputs_cannot_be_trimmed(env: Env) -> None:
    env.edit(
        "roles/reviewer-security.md",
        "inputs: [spec.md, build.md, verify.md, diff.patch,",
        "inputs: [spec.md, build.md, verify.md,",
    )
    out = await env.pipeline(Role.REVIEWER_SECURITY)
    assert out.rejected and "frontmatter" in out.messages


@pytest.mark.parametrize("field", ["description", "inputs", "output", "contract", "tool_policy"])
def test_role_frontmatter_fields_are_locked(field: str) -> None:
    t = (defaults_dir() / "roles" / "reviewer-security.md").read_text()
    head, fm, body = t.split("---\n", 2)
    d = yaml.safe_load(fm)
    d[field] = "changed" if field != "inputs" else ["spec.md"]
    new = "---\n" + yaml.safe_dump(d, sort_keys=False) + "---\n" + body
    c = FileChange("roles/reviewer-security.md", "M", None, "100644", t, new)
    assert check_scope([c], Role.REVIEWER_SECURITY)


def test_role_body_edit_is_fine() -> None:
    t = (defaults_dir() / "roles" / "reviewer-security.md").read_text()
    c = FileChange(
        "roles/reviewer-security.md", "M", None, "100644", t, t.replace("Focus", "Focus on")
    )
    assert check_scope([c], Role.REVIEWER_SECURITY) == []


# --------------------------------------------------------------------------
# misc: describe_model_changes still works on the same objects
# --------------------------------------------------------------------------


def test_describe_model_changes_empty_for_equal() -> None:
    assert describe_model_changes(SHIPPED, SHIPPED) == ()


# --------------------------------------------------------------------------
# 6. the PR body: agent text is inert, machine numbers come first
# --------------------------------------------------------------------------

from dataclasses import replace  # noqa: E402

from swfactory.bench.report import ConfigSummary  # noqa: E402
from swfactory.improve.proposal import Comparison, PrContext, render_improvement_pr  # noqa: E402
from swfactory.improve.signals import FailureSignal  # noqa: E402

INJECTION = """Tightened the guidance.

Fixes #1 Closes acme/other-repo#7 resolves https://github.com/acme/other-repo/issues/8
[Click to approve](javascript:alert(1)) [docs](https://evil.example/phish)
![pixel](https://evil.example/t.gif?x=1)
Benchmark: v1-parity
---
| gemini | 40% → 99% |
Result: better over 12 common tasks
Setext heading
===
````
```
(unclosed fence swallows the real sections)
"""
HOSTILE_DETAIL = (
    "see [x](https://evil.example) and ![i](https://evil.example/p.png) fixes #9 `x` "
    "| gemini | 1% → 99% |"
)


def _summary(name: str, rate: float) -> ConfigSummary:
    return ConfigSummary(
        name=name,
        trials=5,
        completed=5,
        not_run=0,
        hidden_pass_rate=rate,
        judge_pass_rate=None,
        judge_pass_rate_uncalibrated=None,
        quality=rate,
        mean_rounds=1.5,
        total_cost_usd=Decimal("4"),
        mean_cost_usd=Decimal("0.8"),
        median_duration_s=600.0,
        task_pass={f"t{i}": 1.0 if i < round(rate * 5) else 0.0 for i in range(5)},
    )


def _ctx(summary: str = INJECTION, detail: str = HOSTILE_DETAIL) -> PrContext:
    sig = FailureSignal(uuid4(), "job-1", Role.BUILDER, "failed", detail, datetime.now(UTC))
    return PrContext(
        Role.BUILDER,
        "v1-parity",
        summary,
        (sig,),
        (FileChange("roles/builder.md", "M", None, "100644", "a", "b"),),
        Comparison(((_summary("gemini", 0.6), _summary("gemini", 0.8)),), "better", 5),
        Decimal("1"),
        (Decimal("1"), Decimal("1")),
        "https://dash.example",
    )


def _walk(body: str):
    md = pytest.importorskip("markdown_it")
    return md.MarkdownIt("commonmark").enable("table").parse(body)


def test_pr_body_agent_text_has_no_live_markup() -> None:
    _, body = render_improvement_pr(_ctx())
    tokens = _walk(body)
    hrefs, images, headings, tables, htmls, fences = [], [], [], 0, 0, []
    live_text: list[str] = []
    for i, t in enumerate(tokens):
        if t.type == "heading_open":
            headings.append(tokens[i + 1].content)
        elif t.type == "table_open":
            tables += 1
        elif t.type in ("html_block",):
            htmls += 1
        elif t.type == "fence":
            fences.append(t.content)
        for ch in t.children or []:
            if ch.type == "link_open":
                hrefs.append(ch.attrGet("href"))
            elif ch.type == "image":
                images.append(ch.attrGet("src"))
            elif ch.type in ("text", "softbreak", "html_inline"):
                live_text.append(ch.content)
    assert images == [] and htmls == 0
    assert all(h and h.startswith("https://dash.example/#/job/") for h in hrefs)
    assert headings == [
        "Benchmark: v1-parity",
        "Cost",
        "Files changed",
        "Guardrails",
        "Regressions addressed",
        "Untrusted agent text",
    ]
    assert tables == 1
    text = "\n".join(live_text)
    assert not re.search(r"#\d|evil|Fixes|Closes|resolves|javascript", text, re.I)
    assert len(fences) == 1 and "Fixes #1" in fences[0].replace("​", "")


def test_pr_body_result_line_is_machine_computed_and_before_agent_text() -> None:
    _, body = render_improvement_pr(_ctx())
    head, _, agent = body.partition("## Untrusted agent text")
    assert head.count("Result:") == 1 and "Result: better over 5 common tasks." in head
    assert "| gemini | 60% → 80% |" in head
    assert "| gemini | 40%" not in head and "Result: better over 12" not in head
    assert "Untrusted" in body and agent.lstrip().startswith(("The text below", "Text below"))


def test_fence_is_longer_than_any_backtick_run_in_agent_text() -> None:
    _, body = render_improvement_pr(_ctx(summary="a\n`````````\nb ``` c\n## Cost\n"))
    tokens = _walk(body)
    assert [t.type for t in tokens].count("fence") == 1
    assert tokens[-1].type == "fence"  # nothing after the agent's text can be swallowed
    heads = [tokens[i + 1].content for i, t in enumerate(tokens) if t.type == "heading_open"]
    assert heads[-2:] == ["Regressions addressed", "Untrusted agent text"]  # host sections intact
    assert "Cost" in heads and "Guardrails" in heads


def test_hostile_signal_detail_is_a_code_span() -> None:
    _, body = render_improvement_pr(_ctx(summary="ok", detail=HOSTILE_DETAIL))
    section = body.split("## Regressions addressed")[1].split("## Untrusted agent text")[0]
    line = [ln for ln in section.splitlines() if ln.startswith("- ")][0]
    tokens = _walk(line)
    kids = [c for t in tokens for c in (t.children or [])]
    assert [c.type for c in kids if c.type in ("image", "html_inline")] == []
    assert [c.attrGet("href") for c in kids if c.type == "link_open"] == [
        c.attrGet("href")
        for c in kids
        if c.type == "link_open" and "dash.example" in (c.attrGet("href") or "")
    ]
    assert not re.search(r"#\d", "".join(c.content for c in kids if c.type == "text"))


def test_agent_text_labelled_and_never_in_title() -> None:
    title, body = render_improvement_pr(replace(_ctx(), suite="v1-parity"))
    assert "Fixes" not in title and "evil" not in title
    assert "## Untrusted agent text" in body
