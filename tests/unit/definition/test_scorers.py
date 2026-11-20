from __future__ import annotations

from swfactory.definition.loader import defaults_dir, load_definition
from swfactory.definition.models import JudgeEntry, ScorerDef, validate_definition

MIN_SCORER = {
    "labels": [
        {"value": "A", "score": 1.0, "description": "good"},
        {"value": "B", "score": 0.0, "description": "bad"},
    ],
    "passing_score": 0.5,
    "judge": "claude-haiku@vertex",
}


def test_sampling_rate_is_m1_fraction():
    assert ScorerDef.model_validate(MIN_SCORER).sampling_rate == 0.10


def test_alias_string_parses_as_entry():
    s = ScorerDef.model_validate(MIN_SCORER)
    assert s.judge == JudgeEntry(model="claude-haiku@vertex")
    assert s.judge.fallback == () and s.judge.engine == "direct"


def test_entry_mapping_parses():
    s = ScorerDef.model_validate(
        {**MIN_SCORER, "judge": {"model": "a@v", "fallback": ["b@v"], "engine": "direct"}}
    )
    assert s.judge == JudgeEntry("a@v", ("b@v",))


def test_starter_scorers():
    d = load_definition(str(defaults_dir())).definition
    assert d is not None
    s = d.scorers
    assert set(s) == {"merge-readiness", "spec-adherence", "scope-creep", "process-compliance"}
    assert all(x.applies_to == ("builder",) and x.sampling_rate == 0.10 for x in s.values())
    assert all(x.judge.model == "claude-haiku@vertex" for x in s.values())
    assert all(x.judge.fallback == ("gemini-lite@vertex",) for x in s.values())
    assert {k: x.passing_score for k, x in s.items()} == {
        "merge-readiness": 0.7,
        "spec-adherence": 1.0,
        "scope-creep": 0.6,
        "process-compliance": 1.0,
    }


def test_unknown_fallback_alias_is_an_issue():
    d = load_definition(str(defaults_dir())).definition
    assert d is not None
    data = d.model_dump(mode="json", by_alias=True)
    data["scorers"]["merge-readiness"]["judge"]["fallback"] = ["ghost@vertex"]
    issues = validate_definition(data)[1]
    assert [i.loc for i in issues] == [("scorers", "merge-readiness", "judge", "fallback", 0)]
