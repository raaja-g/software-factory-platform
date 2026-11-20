import pytest

from swfactory.roles.contracts import ContractError, check_output, parse_triage


def test_parse_triage():
    text = "kind: bugfix\nsize: S\nrisk: low\ntouches_ui: false\n\n- because ..."
    assert parse_triage(text) == {"kind": "bugfix", "size": "S", "risk": "low", "touches_ui": False}


def test_parse_triage_true_and_decoration():
    got = parse_triage("- kind: docs\n- size: L\n- risk: high\n- touches_ui: true")
    assert got == {"kind": "docs", "size": "L", "risk": "high", "touches_ui": True}
    assert list(got) == ["kind", "size", "risk", "touches_ui"]


@pytest.mark.parametrize(
    "text",
    [
        "**Kind:** bugfix\nsize: S\nrisk: low\ntouches_ui: false",
        "kind: chore\nsize: S\nrisk: low\ntouches_ui: no",
        "kind: bugfix\nsize: S\nrisk: low",
        "kind: bugfix\nkind: docs\nsize: S\nrisk: low\ntouches_ui: true",
    ],
)
def test_near_miss_raises(text):
    with pytest.raises(ContractError):
        parse_triage(text)
    assert check_output("triage", text) is not None


def test_check_output_accepts_valid():
    assert check_output("triage", "kind: docs\nsize: S\nrisk: low\ntouches_ui: false") is None
