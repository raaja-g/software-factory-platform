from decimal import Decimal

from swfactory.definition.models import AppDef, TriageConfig, TriageRule
from swfactory.domain.types import DEFAULT_REVIEWERS
from swfactory.models.triage import (
    mentioned_paths,
    render_triage_md,
    triage_by_rules,
    triage_from_json,
    triage_to_json,
)


def app(**kw) -> AppDef:
    return AppDef(repo_kind="local", repo_url="demo", **kw)


def rule(**kw) -> TriageRule:
    m = {k: tuple(kw.pop(k)) for k in ("keywords", "globs", "labels") if k in kw}
    return TriageRule.model_validate({**kw, "match": m})


def cfg(*rules) -> TriageConfig:
    return TriageConfig(rules=rules)


def test_empty_rules_is_v1_parity():
    r, _ = triage_by_rules(TriageConfig(), app(), text="Add a page", labels=[])
    assert (r.kind, r.size, r.risk, r.touches_ui, r.profile_name, r.budget_usd) == (
        "feature", "M", "low", False, None, None,
    )  # fmt: skip
    assert r.reviewers == DEFAULT_REVIEWERS and r.require_human_approval is False
    assert set(r.source.values()) == {"rules"}


def test_keyword_rule_whole_word_case_insensitive():
    c = cfg(rule(name="ui-words", keywords=["page"], set={"touches_ui": True}))
    assert triage_by_rules(c, app(), text="Add a Page", labels=[])[0].touches_ui is True
    assert triage_by_rules(c, app(), text="Fix paged results", labels=[])[0].touches_ui is False


def test_label_rule_and_later_rule_wins():
    c = cfg(
        rule(labels=["ui"], set={"touches_ui": True, "size": "S"}),
        rule(labels=["ui"], set={"size": "L"}),
    )
    r, reasons = triage_by_rules(c, app(), text="x", labels=["ui"])
    assert (r.touches_ui, r.size) == (True, "L") and '- rule #2 matched label "ui"' in reasons


def test_glob_rule_matches_paths_in_text():
    c = cfg(rule(name="styles", globs=["**/*.css"], set={"touches_ui": True}))
    r, reasons = triage_by_rules(c, app(), text="Fix spacing in `web/app.css`.", labels=[])
    assert r.touches_ui is True
    assert '- rule "styles" matched glob "**/*.css" on web/app.css' in reasons
    assert triage_by_rules(c, app(), text="Fix spacing", labels=[])[0].touches_ui is False


def test_mentioned_paths():
    assert mentioned_paths("Fix `web/app.css`, then README.md.") == ["web/app.css", "README.md"]


def test_profile_outside_allowed_is_dropped():
    r, reasons = triage_by_rules(
        cfg(rule(name="big", profile="nope")),
        app(allowed_profiles=("team-gemini",)),
        text="x",
        labels=[],
    )
    assert r.profile_name is None
    assert '- rule "big" profile "nope" is not allowed for this app; ignored' in reasons


def test_budget_clamped_to_app_budget():
    r, _ = triage_by_rules(
        cfg(rule(budget_usd=Decimal("50"))), app(budget_usd=Decimal("10")), text="x", labels=[]
    )
    assert r.budget_usd == Decimal("10")


def test_triage_md_lists_reasons():
    r, reasons = triage_by_rules(
        cfg(rule(name="ui-words", keywords=["page"])), app(), text="Add a page", labels=[]
    )
    md = render_triage_md(r, reasons)
    assert "kind: feature" in md.splitlines()
    assert '- rule "ui-words" matched keyword "page"' in md.splitlines()
    assert triage_from_json(triage_to_json(r)) == r
