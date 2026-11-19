from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from swfactory.github.checks import CHECK_NAME, PrBodyContext, render_pr_body, review_check


def make(**kw) -> PrBodyContext:
    base = PrBodyContext(
        job_id=uuid4(),
        feature="# Add X\nplease",
        spec_summary="Spec",
        criteria=("Returns 200", "Logs it"),
        verify="ok",
        screenshots=("https://x/s.png",),
        verdicts={1: {"ux": "PASS", "code": "CHANGES"}, 2: {"ux": "PASS", "code": "PASS"}},
        decision="APPROVE",
        decision_reason="all good",
        runs=(("builder", "claude", "m", Decimal("1.5")),),
        dashboard_url="https://dash/jobs/1",
    )
    return replace(base, **kw)


def test_pr_body_sections_and_marker():
    ctx = make()
    body = render_pr_body(ctx)
    for h in (
        "## Feature",
        "## Acceptance criteria",
        "## Verify",
        "## Reviews",
        "## Decision",
        "## Models and cost",
    ):
        assert h in body
    assert f"<!-- factory:job={ctx.job_id} -->" in body and "- [ ] Returns 200" in body
    assert "| 1 | PASS | CHANGES |" in body and ctx.dashboard_url in body
    assert "\n# Add X" not in body and "> # Add X" in body


def test_pr_body_trimmed_with_omitted_note():
    huge = make(
        verify="x" * 50_000, verdicts={i: {"ux": "PASS", "code": "PASS"} for i in range(1, 400)}
    )
    out = render_pr_body(huge, limit=2000)
    assert "omitted" in out and len(out) <= 2000
    assert "## Decision" in out


def test_check_conclusions():
    approved = make()
    escalated = make(decision="ESCALATE", verdicts={1: {"ux": "CHANGES"}})
    err = make(verdicts={1: {"ux": "ERROR"}})
    assert CHECK_NAME == "factory/review"
    assert (
        review_check(approved)[0] == "success" and review_check(escalated)[0] == "action_required"
    )
    assert review_check(err)[0] == "failure"
    human = replace(escalated, approved_by="github:ana")
    assert review_check(human)[0] == "success" and "github:ana" in review_check(human)[2]
