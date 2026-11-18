"""Shared domain types: enums, stage sets and the agent task/result dataclasses."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID


class Stage(StrEnum):
    QUEUED = "queued"
    TRIAGE = "triage"
    SPEC = "spec"
    BUILD = "build"
    VERIFY = "verify"
    REVIEW = "review"
    APPROVE = "approve"
    NEEDS_HUMAN = "needs_human"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    MERGED = "merged"
    CLOSED = "closed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STAGES = frozenset(
    {
        Stage.TRIAGE,
        Stage.SPEC,
        Stage.BUILD,
        Stage.VERIFY,
        Stage.REVIEW,
        Stage.APPROVE,
        Stage.DELIVERING,
    }
)
WAITING_STAGES = frozenset({Stage.NEEDS_HUMAN, Stage.FAILED, Stage.DELIVERED})
TERMINAL_STAGES = frozenset({Stage.MERGED, Stage.CLOSED, Stage.CANCELLED})


class Role(StrEnum):
    TRIAGE = "triage"
    REQUIREMENTS = "requirements"
    SPEC_WRITER = "spec-writer"
    BUILDER = "builder"
    REVIEWER_SECURITY = "reviewer-security"
    REVIEWER_UX = "reviewer-ux"
    REVIEWER_UI = "reviewer-ui"
    REVIEWER_CODE = "reviewer-code"
    APPROVER = "approver"
    JUDGE = "judge"
    IMPROVER = "improver"


DEFAULT_REVIEWERS: tuple[str, ...] = ("security", "ux", "ui", "code")


def reviewer_role(kind: str) -> Role:
    """``"ux"`` -> ``Role.REVIEWER_UX``; ValueError for a kind outside DEFAULT_REVIEWERS."""
    if kind not in DEFAULT_REVIEWERS:
        raise ValueError(f"unknown reviewer kind: {kind!r}")
    return Role(f"reviewer-{kind}")


class Verdict(StrEnum):
    PASS = "PASS"
    CHANGES = "CHANGES"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class Decision(StrEnum):
    APPROVE = "APPROVE"
    ESCALATE = "ESCALATE"


class HumanAction(StrEnum):
    APPROVE = "approve"
    REWORK = "rework"
    REJECT = "reject"
    RETRY = "retry"
    ADD_BUDGET = "add_budget"
    CANCEL = "cancel"


class MergePolicy(StrEnum):
    LOCAL_MERGE = "local_merge"
    PR_MANUAL = "pr_manual"
    PR_AUTO_MERGE = "pr_auto_merge"


class SelectionMode(StrEnum):
    FIXED = "fixed"
    SHADOW = "shadow"
    LAYA = "laya"


class TriageMode(StrEnum):
    RULES = "rules"
    SHADOW = "shadow"
    LAYA = "laya"
    LLM = "llm"


class ToolPolicy(StrEnum):
    # narrowest first. read_run = read files and run commands, never edit;
    # allowed only in an isolated container
    NONE = "none"
    READ_ONLY = "read_only"
    READ_RUN = "read_run"
    WRITE = "write"


class EngineSupport(StrEnum):
    OK = "ok"
    EXPERIMENTAL = "experimental"
    NO = "no"


class RunStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    BUDGET = "budget"


class CostSource(StrEnum):
    GATEWAY = "gateway"
    ENGINE = "engine"
    ESTIMATE = "estimate"


class JobKind(StrEnum):
    NORMAL = "normal"
    BENCHMARK = "benchmark"
    IMPROVER = "improver"
    REQUIREMENTS = "requirements"


class BacklogStatus(StrEnum):
    DRAFTING = "drafting"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    CANCELLED = "cancelled"


class VerifyStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class FactoryMode(StrEnum):
    PERSONAL = "personal"
    SERVER = "server"


@dataclass(frozen=True, slots=True)
class ModelRef:
    alias: str  # "gemini-pro@vertex": key under models: in factory.yaml
    provider: str  # key under providers:
    upstream_id: str  # the provider's model id


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AgentTask:
    job_id: UUID
    run_id: UUID
    role: Role
    round: int
    attempt: int
    engine: str  # "opencode" | "claude_code" | "gemini_cli" | "direct" | "fake"
    model: ModelRef
    system_prompt: str
    prompt: str
    workdir: str | None  # inside the sandbox; None for direct
    output_path: str | None  # /work/.factory/out/<run_id>.md; None for direct
    tool_policy: ToolPolicy
    attachments: tuple[str, ...]  # sandbox paths of images for multimodal roles
    max_turns: int
    timeout_s: int
    resume_session: str | None
    gateway_base_url: str
    gateway_key: str
    tags: Mapping[str, str]  # app, job, role, round, run
    max_budget_usd: Decimal | None = None  # the run key's cap


@dataclass(frozen=True, slots=True)
class AgentResult:
    status: RunStatus
    output: str | None  # write roles: the output file's text; others: the engine's reply
    session_id: str | None
    usage: Usage
    used_models: tuple[str, ...]  # models the engine reports it actually used
    reported_cost_usd: Decimal | None  # per-run delta, never a cumulative total
    transcript: str | None  # redacted transcript text (stored as an artifact)
    error: str | None  # gateway / HTTP / CLI error text when status is not OK
    engine_version: str
    meta: Mapping[str, Any] = field(default_factory=dict)  # includes "error_class" on failures
