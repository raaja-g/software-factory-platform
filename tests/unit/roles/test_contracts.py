from pathlib import Path

import pytest

from swfactory.domain.types import Decision, Verdict
from swfactory.roles.contracts import (
    ContractError,
    check_output,
    check_requirements,
    check_spec,
    parse_decision,
    parse_requirements,
    parse_verdict,
)

V1 = Path("tests/fixtures/v1")


@pytest.mark.parametrize(
    "text,want",
    [
        ("VERDICT: PASS\n\n## Summary", Verdict.PASS),
        ("  ## **VERDICT: CHANGES**  \n", Verdict.CHANGES),
        ("> verdict: pass", Verdict.PASS),
        ("\n\nVERDICT: CHANGES", Verdict.CHANGES),
    ],
)
def test_verdict_accepts(text, want):
    assert parse_verdict(text) is want


@pytest.mark.parametrize(
    "text",
    [
        "**Verdict:** pass",
        "Here is my review.\nVERDICT: PASS",
        "VERDICT: PASSED",
        "VERDICT: PASS.",
        "Verdict - PASS",
        "PASS",
        "VERDICT: APPROVE",
        "",
        "   \n",
    ],
)
def test_review_focus_1_near_miss_verdicts_raise(text):
    with pytest.raises(ContractError):
        parse_verdict(text)


@pytest.mark.parametrize(
    "text,want",
    [
        ("APPROVE\n\n## Reasoning", Decision.APPROVE),
        ("_escalate_", Decision.ESCALATE),
        ("\n## approve\n", Decision.APPROVE),
    ],
)
def test_decision_accepts(text, want):
    assert parse_decision(text) is want


@pytest.mark.parametrize(
    "text", ["APPROVED", "Decision: APPROVE", "I APPROVE this", "approve with nits", ""]
)
def test_review_focus_1_near_miss_decisions_raise(text):
    with pytest.raises(ContractError):
        parse_decision(text)


def test_v1_fixtures():
    assert all(
        parse_verdict(p.read_text()) in (Verdict.PASS, Verdict.CHANGES)
        for p in V1.glob("*/round-*/review-*.md")
    )
    assert (
        parse_verdict((V1 / "003-todo-web/round-1/review-security.md").read_text())
        is Verdict.CHANGES
    )
    assert {parse_decision(p.read_text()) for p in V1.glob("*/decision.md")} == {Decision.APPROVE}
    assert [check_spec(p.read_text()) for p in sorted(V1.glob("*/spec.md"))] == [[], [], [], []]


def test_check_spec_problems():
    assert check_spec("# Spec\n- [ ] x") == ["missing '## Acceptance criteria' section"]
    assert check_spec("### Acceptance criteria\n- [ ] x") == [
        "missing '## Acceptance criteria' section"
    ]
    assert check_spec("## Acceptance criteria\n- [x] done\n## Notes\n- [ ] later") == [
        "no '- [ ]' item under '## Acceptance criteria'"
    ]


GOOD = Path("tests/fixtures/requirements/good.md").read_text()


def test_parse_requirements_items():
    items = parse_requirements(GOOD)
    assert [(i.id, i.title, i.depends_on) for i in items] == [
        ("setup", "Set up the project", ()),
        ("add", "Add todos", ("setup",)),
    ]
    assert items[1].acceptance == ("POST /todos creates one", "GET /todos lists them")
    assert items[1].feature == "Add and list todos."


def test_check_requirements_accepts_a_good_document():
    assert check_requirements(GOOD) == []


@pytest.mark.parametrize(
    "old,new,problem",
    [
        ("## Out of scope\nAccounts and sharing.\n\n", "", "missing '## Out of scope' section"),
        ("## Backlog", "### Backlog", "missing '## Backlog' section"),
        ("id: add", "id: setup", "duplicate id 'setup'"),
        (
            "depends_on: [setup]",
            "depends_on: [later]",
            "item 'add' depends on 'later', which is not an earlier item",
        ),
        (
            'acceptance: ["POST /todos creates one", "GET /todos lists them"]',
            "acceptance: []",
            "item 'add' has no acceptance criterion",
        ),
        ("  title: Add todos\n", "", "item 'add' is missing 'title'"),
        (
            "  feature: Add and list todos.\n",
            "  feature: [unclosed\n",
            "the backlog block is not valid yaml",
        ),
    ],
)
def test_check_requirements_problems(old, new, problem):
    assert old in GOOD
    assert check_requirements(GOOD.replace(old, new)) == [problem]


def test_check_requirements_needs_exactly_one_yaml_block():
    assert check_requirements(GOOD + "\n```yaml\n- id: x\n```\n") == [
        "'## Backlog' must hold exactly one yaml block"
    ]
    assert check_requirements(GOOD.replace("```yaml", "```")) == [
        "'## Backlog' must hold exactly one yaml block"
    ]


def test_check_requirements_empty_and_too_many():
    assert check_requirements(GOOD.split("```yaml")[0] + "```yaml\n[]\n```\n") == [
        "the backlog has no items"
    ]
    assert check_requirements(GOOD, max_items=1) == [
        "the backlog has 2 items; at most 1 are allowed"
    ]


def test_parse_requirements_raises_the_first_problem():
    with pytest.raises(ContractError, match="duplicate id 'setup'"):
        parse_requirements(GOOD.replace("id: add", "id: setup"))


def test_check_output_requirements():
    assert check_output("requirements", GOOD) is None
    assert check_output("requirements", None) == "no output file was written"
    assert (
        check_output("requirements", GOOD, max_items=1)
        == "the backlog has 2 items; at most 1 are allowed"
    )


def test_check_output():
    assert check_output("verdict", None) == "no output file was written"
    assert check_output("none", "  \n") == "the output is empty"
    assert check_output("verdict", "VERDICT: PASS") is None
