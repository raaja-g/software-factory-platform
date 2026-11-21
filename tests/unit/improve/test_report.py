from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from swfactory.bench.report import ConfigSummary
from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.definition.models import Definition
from swfactory.domain.types import Role
from swfactory.improve.proposal import (
    REGRESSIONS_HEADING,
    Comparison,
    FileChange,
    PrContext,
    compare,
    describe_model_changes,
    render_improvement_pr,
)
from swfactory.improve.signals import FailureSignal

TASKS = ("t1", "t2", "t3", "t4", "t5")


def st(name: str, rate: float, cost: str) -> ConfigSummary:
    n = round(rate * 5)
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
        total_cost_usd=Decimal(cost),
        mean_cost_usd=Decimal(cost) / 5,
        median_duration_s=600.0,
        task_pass={t: (1.0 if i < n else 0.0) for i, t in enumerate(TASKS)},
    )


def without_task(c: ConfigSummary, t: str) -> ConfigSummary:
    return replace(
        c, completed=c.completed - 1, task_pass={k: v for k, v in c.task_pass.items() if k != t}
    )


def sig(i: int = 14, detail: str = "failed on tests") -> FailureSignal:
    return FailureSignal(
        uuid4(), f"todo:{i:03d}", Role.BUILDER, "failed", detail, datetime.now(UTC)
    )


CTX = PrContext(
    role=Role.BUILDER,
    suite="v1-parity",
    summary="Tighten the builder guidance.",
    signals=(sig(),),
    changes=(FileChange("roles/builder.md", "M", None, "100644", "a", "b"),),
    comparison=Comparison(((st("gemini", 0.6, "4.00"), st("gemini", 0.8, "4.50")),), "better", 5),
    improver_cost_usd=Decimal("40.10"),
    bench_cost_usd=(Decimal("4.00"), Decimal("4.50")),
    dashboard_url="http://localhost:8080",
)


def test_better_when_hidden_pass_rises():
    assert compare([st("gemini", 0.4, "4.00")], [st("gemini", 0.8, "4.50")]).result == "better"


def test_a_one_task_flip_is_inconclusive():
    assert compare([st("g", 0.6, "4.00")], [st("g", 0.8, "4.00")]).result == "inconclusive"
    assert compare([st("g", 0.8, "4.00")], [st("g", 0.6, "4.00")]).result == "inconclusive"


def test_too_few_common_tasks_is_inconclusive():
    def few(c):
        return replace(c, task_pass={k: v for k, v in c.task_pass.items() if k in ("t1", "t2")})

    r = compare([few(st("g", 0.4, "4"))], [few(st("g", 1.0, "4"))])
    assert r.result == "inconclusive" and r.common_tasks == 2


def test_a_tiny_saving_at_equal_quality_is_not_better():
    assert compare([st("g", 0.8, "4.00")], [st("g", 0.8, "3.95")]).result == "same"


def test_profile_edit_the_benchmark_does_not_run_is_unmeasured():
    r = compare([st("g", 0.4, "4")], [st("g", 0.8, "4")], exercised=False)
    assert r.result == "unmeasured"


def test_better_when_equal_quality_cheaper():
    assert compare([st("g", 0.8, "4.00")], [st("g", 0.8, "3.00")]).result == "better"


def test_reviewer_group_never_better():
    r = compare([st("g", 0.8, "4.00")], [st("g", 0.8, "3.00")], role=Role.REVIEWER_SECURITY)
    assert r.result == "unmeasured"


def test_reviewer_group_still_worse():
    assert compare([st("g", 0.8, "4")], [st("g", 0.4, "1")], role=Role.APPROVER).result == "worse"


def test_unmeasured_title():
    c = replace(
        CTX, role=Role.REVIEWER_SECURITY, comparison=Comparison(CTX.comparison.rows, "unmeasured")
    )
    title, body = render_improvement_pr(c)
    assert title == "Improve reviewer-security guidance (v1-parity: review strictness not measured)"
    assert (
        "Hidden checks do not measure review strictness; judge this change by reading it." in body
    )


def test_worse_when_hidden_pass_falls():
    assert compare([st("g", 0.8, "4")], [st("g", 0.4, "1")]).result == "worse"


def test_compares_only_tasks_done_on_both_sides():
    r = compare([st("g", 0.8, "4.00")], [without_task(st("g", 0.8, "4.00"), "t5")])
    assert r.result == "same" and r.common_tasks == 4


def test_unpaired_names_left_out():
    r = compare([st("a", 0.8, "4"), st("b", 0.2, "4")], [st("a", 0.8, "4")])
    assert len(r.rows) == 1


def test_body_sections():
    title, body = render_improvement_pr(CTX)
    assert title == "Improve builder guidance (v1-parity: better)"
    # machine-computed sections come first; the signals and the agent's text come after them
    assert (
        body.index("## Benchmark: v1-parity")
        < body.index("## Cost")
        < body.index(REGRESSIONS_HEADING)
        < body.index("## Untrusted agent text")
    )
    assert "| Configuration | Hidden checks | Judge pass | Rounds | Cost | Time |" in body
    assert "| gemini | 60% → 80% |" in body
    assert "todo:014" in body and "Total: $48.60" in body and "The improver never merges" in body


def test_agent_text_is_neutralised():
    c = replace(CTX, summary="hi @octocat\n# Big <script>", signals=(sig(detail="x | y @bob"),))
    _, body = render_improvement_pr(c)
    assert "@octocat" not in body and "@bob" not in body and "<script>" not in body


def test_bad_dashboard_url_gives_no_link():
    _, body = render_improvement_pr(replace(CTX, dashboard_url="javascript:alert(1)"))
    assert "javascript" not in body


def _defs() -> tuple[Definition, Definition]:
    r = load_definition(str(defaults_dir()))
    assert r.definition is not None
    base = r.definition
    prof = base.profiles["claude-vertex"]
    rp = prof.roles[Role.APPROVER]
    assert rp.model == "claude-sonnet@vertex", rp.model
    new = prof.model_copy(
        update={
            "roles": {
                **prof.roles,
                Role.APPROVER: rp.model_copy(update={"model": "claude-opus@vertex"}),
            }
        }
    )
    return base, base.model_copy(update={"profiles": {**base.profiles, "claude-vertex": new}})


LINE = (
    "- profiles.claude-vertex.approver.model: "
    "claude-sonnet@vertex \u2192 claude-opus@vertex (claude, strong)"
)


def test_guardrails_lists_model_changes():
    base, swapped = _defs()
    assert describe_model_changes(base, swapped) == (LINE,)
    assert describe_model_changes(base, base) == ()
    body = render_improvement_pr(replace(CTX, model_changes=(LINE,)))[1]
    assert LINE in body.split("## Guardrails")[1]


def test_long_evidence_trimmed():
    c = replace(CTX, signals=tuple(sig(i, "d" * 300) for i in range(1000)), summary="s" * 100)
    body = render_improvement_pr(c)[1]
    assert len(body) <= 60_000 and "more signals omitted" in body


def test_huge_summary_trimmed():
    body = render_improvement_pr(replace(CTX, summary="s" * 200_000))[1]
    assert len(body) <= 60_000
