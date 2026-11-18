from swfactory.definition.models import AppDef
from swfactory.domain.types import DEFAULT_REVIEWERS
from swfactory.models.triage import ReviewerPlan, TriageResult, matches_glob, reviewers_for


def app(**kw) -> AppDef:
    return AppDef(repo_kind="local", repo_url="demo", **kw)


def tr(**kw) -> TriageResult:
    base = dict(
        kind="feature", size="M", risk="low", touches_ui=False, source={},
        profile_name=None, budget_usd=None, reviewers=DEFAULT_REVIEWERS,
        require_human_approval=False,
    )  # fmt: skip
    base.update(kw)
    return TriageResult(**base)  # type: ignore[arg-type]


def test_security_never_skipped():
    plan = reviewers_for(app(reviewer_relevance=True), tr(reviewers=("code",)), ["src/a.py"])
    assert plan.run == ("security", "code")


def test_ui_forced_by_ui_globs():
    plan = reviewers_for(app(), tr(reviewers=("security", "code")), ["web/index.html"])
    assert plan.run == ("security", "ui", "code")


def test_relevance_off_by_default():
    assert reviewers_for(app(), tr(), ["src/a.py"]) == ReviewerPlan(
        run=DEFAULT_REVIEWERS, skipped={}
    )


def test_relevance_on_skips_ux_ui_for_backend():
    plan = reviewers_for(app(reviewer_relevance=True), tr(), ["src/a.py"])
    assert plan.run == ("security", "code")
    assert plan.skipped == {"ux": "no UI files changed", "ui": "no UI files changed"}


def test_relevance_keeps_ux_when_touches_ui():
    plan = reviewers_for(app(reviewer_relevance=True), tr(touches_ui=True), ["src/a.py"])
    assert plan.run == DEFAULT_REVIEWERS


def test_matches_glob_double_star():
    assert (
        matches_glob("index.html", "**/*.html"),
        matches_glob("a/b/index.html", "**/*.html"),
        matches_glob("a/b.py", "**/*.html"),
    ) == (True, True, False)
