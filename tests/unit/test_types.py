import dataclasses

import pytest

from swfactory.domain.types import (
    ACTIVE_STAGES,
    DEFAULT_REVIEWERS,
    TERMINAL_STAGES,
    WAITING_STAGES,
    AgentResult,
    AgentTask,
    BacklogStatus,
    FactoryMode,
    JobKind,
    MergePolicy,
    ModelRef,
    Role,
    RunStatus,
    Stage,
    ToolPolicy,
    Usage,
    Verdict,
    reviewer_role,
)


def test_reviewer_role():
    assert [reviewer_role(k) for k in DEFAULT_REVIEWERS] == [
        Role.REVIEWER_SECURITY,
        Role.REVIEWER_UX,
        Role.REVIEWER_UI,
        Role.REVIEWER_CODE,
    ]
    with pytest.raises(ValueError):
        reviewer_role("perf")


def test_stage_sets_partition():
    assert not (ACTIVE_STAGES & WAITING_STAGES)
    assert not (ACTIVE_STAGES & TERMINAL_STAGES)
    assert not (WAITING_STAGES & TERMINAL_STAGES)
    assert ACTIVE_STAGES | WAITING_STAGES | TERMINAL_STAGES | {Stage.QUEUED} == set(Stage)


def test_enum_values_are_wire_values():
    assert Stage.NEEDS_HUMAN == "needs_human"
    assert Role.SPEC_WRITER == "spec-writer"
    assert Role.REQUIREMENTS == "requirements"
    assert JobKind.REQUIREMENTS == "requirements"
    assert BacklogStatus.AWAITING_APPROVAL == "awaiting_approval"
    assert MergePolicy.PR_AUTO_MERGE == "pr_auto_merge"
    assert [v.value for v in Verdict] == ["PASS", "CHANGES", "ERROR", "SKIPPED"]
    assert FactoryMode("server") is FactoryMode.SERVER
    assert [p.value for p in ToolPolicy] == ["none", "read_only", "read_run", "write"]


def test_dataclasses_frozen_with_slots():
    u = Usage()
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (
        0,
        0,
        0,
        0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.input_tokens = 1  # type: ignore[misc]
    assert all("__slots__" in vars(c) for c in (Usage, ModelRef, AgentTask, AgentResult))
    assert not hasattr(u, "__dict__")
    r = AgentResult(RunStatus.OK, "x", None, Usage(), (), None, None, None, "1.0")
    other = AgentResult(RunStatus.OK, "y", None, Usage(), (), None, None, None, "1.0")
    assert r.meta == {} and r.meta is not other.meta
