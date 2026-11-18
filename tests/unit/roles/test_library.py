from pathlib import Path

import pytest
import yaml

from swfactory.definition.models import RoleFile
from swfactory.domain.types import DEFAULT_REVIEWERS, Role, ToolPolicy, reviewer_role
from swfactory.roles.library import (
    DEFAULT_ROLES_DIR,
    RoleFileError,
    RoleLibrary,
    effective_tool_policy,
    parse_role_file,
)

lib = RoleLibrary()


def body(r):
    return lib.get(r, definition_sha="x").body


def shipped_text(r):
    return (Path(DEFAULT_ROLES_DIR) / f"{r.value}.md").read_text()


def shipped_frontmatter(r):
    return yaml.safe_load(shipped_text(r).split("---\n")[1])


def test_all_default_roles_parse():
    assert {lib.get(r, definition_sha="x").role for r in Role} == set(Role)


def test_contracts_and_policies():
    got = {
        r: (lib.get(r, definition_sha="x").contract, lib.get(r, definition_sha="x").tool_policy)
        for r in Role
    }
    assert got == {
        Role.TRIAGE: ("triage", ToolPolicy.NONE),
        Role.REQUIREMENTS: ("requirements", ToolPolicy.READ_ONLY),
        Role.SPEC_WRITER: ("spec", ToolPolicy.READ_ONLY),
        Role.BUILDER: ("none", ToolPolicy.WRITE),
        Role.APPROVER: ("decision", ToolPolicy.READ_ONLY),
        Role.JUDGE: ("none", ToolPolicy.NONE),
        Role.IMPROVER: ("none", ToolPolicy.WRITE),
        **{reviewer_role(k): ("verdict", ToolPolicy.READ_RUN) for k in DEFAULT_REVIEWERS},
    }


def test_reviewer_bodies_may_run_but_never_edit():
    for k in DEFAULT_REVIEWERS:
        assert "start the app in your isolated container" in body(reviewer_role(k))
        assert "Never edit files." in body(reviewer_role(k))


def test_bodies_are_engine_neutral():
    for r in Role:
        bad = [
            s
            for s in ("factory/jobs/", "subagent", "SendMessage", "git checkout -b")
            if s in body(r)
        ]
        assert not bad
        assert "{{in_dir}}" in body(r) and "{{output_instruction}}" in body(r)
    assert all(
        "AGENTS.md" in body(r)
        for r in (
            Role.REQUIREMENTS,
            Role.SPEC_WRITER,
            Role.BUILDER,
            *map(reviewer_role, DEFAULT_REVIEWERS),
        )
    )


def test_v1_output_formats_kept():
    assert all(
        "VERDICT: PASS | CHANGES" in body(reviewer_role(k))
        and "## Required changes" in body(reviewer_role(k))
        for k in DEFAULT_REVIEWERS
    )
    assert "APPROVE | ESCALATE" in body(Role.APPROVER)
    assert "## For the human" in body(Role.APPROVER)
    assert "## Acceptance criteria" in body(Role.SPEC_WRITER)


def test_requirements_role_output_format():
    b = body(Role.REQUIREMENTS)
    assert all(h in b for h in ("## Summary", "## Requirements", "## Out of scope", "## Backlog"))
    assert "```yaml" in b and all(k in b for k in ("depends_on", "acceptance", "feature"))
    assert "On an empty repo, the first item sets up the project." in b
    assert "No code changes." in b
    assert "rework-*.md" in " ".join(lib.get(Role.REQUIREMENTS, definition_sha="x").inputs)


def test_feature_roles_read_the_requirements():
    assert all(
        "requirements.md" in lib.get(r, definition_sha="x").inputs
        for r in (Role.SPEC_WRITER, Role.BUILDER)
    )


def test_builder_flags_test_command_change():
    assert "## Test command changed" in body(Role.BUILDER)


def test_override_at_sha_wins():
    rf = RoleFile(
        frontmatter=shipped_frontmatter(Role.REVIEWER_CODE),
        body="Code review. {{in_dir}} {{output_instruction}}",
    )
    lib2 = RoleLibrary(overrides_for=lambda sha: {"abc": {Role.REVIEWER_CODE: rf}}.get(sha, {}))
    assert (
        lib2.get(Role.REVIEWER_CODE, definition_sha="abc").body
        == "Code review. {{in_dir}} {{output_instruction}}"
    )
    assert lib2.get(Role.REVIEWER_CODE, definition_sha="def").body == body(Role.REVIEWER_CODE)


def test_override_cannot_change_contract():
    rf = RoleFile(
        frontmatter={**shipped_frontmatter(Role.REVIEWER_CODE), "contract": "none"},
        body=body(Role.REVIEWER_CODE),
    )
    with pytest.raises(RoleFileError, match="override may not change contract"):
        RoleLibrary(overrides_for=lambda sha: {Role.REVIEWER_CODE: rf}).get(
            Role.REVIEWER_CODE, definition_sha="abc"
        )


def test_unknown_placeholder_rejected():
    with pytest.raises(RoleFileError, match=r"unknown placeholder \{\{secret\}\}"):
        parse_role_file(shipped_text(Role.JUDGE) + "\n{{secret}}", role=Role.JUDGE)


N, RO, RR, W = ToolPolicy.NONE, ToolPolicy.READ_ONLY, ToolPolicy.READ_RUN, ToolPolicy.WRITE


@pytest.mark.parametrize(
    "role_policy,requested,want",
    [
        (W, RO, RO),
        (RO, W, RO),
        (N, W, N),
        (W, W, W),
        (RR, W, RR),
        (RR, RO, RO),
        (RR, N, N),
        (RR, RR, RR),
        (W, RR, RR),
        (RO, RR, RO),
    ],
)
def test_effective_tool_policy_narrower_wins(role_policy, requested, want):
    assert effective_tool_policy(role_policy, requested) is want
