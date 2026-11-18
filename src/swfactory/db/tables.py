"""ORM tables of schema ``app`` (section 15). Alembic's 0001_initial creates them."""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from swfactory.domain.types import (
    BacklogStatus,
    CostSource,
    Decision,
    HumanAction,
    JobKind,
    RunStatus,
    Stage,
    Verdict,
)

SCHEMA = "app"
JOB_SOURCES = ("dashboard", "cli", "github_issue", "schedule", "webhook", "backlog", "api")
USER_ROLES = ("viewer", "submitter", "approver", "admin")
ARTIFACT_KINDS = ("md", "log", "transcript", "image", "json")
REPO_KINDS = ("github", "local")
DECISION_PURPOSES = ("model", "triage")
BENCH_STATUSES = ("pending", "running", "done", "failed", "cancelled")


class XID8(UserDefinedType[Any]):
    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "xid8"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


def _in(column: str, values: "type[enum.StrEnum] | tuple[str, ...]", nullable: bool = False) -> str:
    items = [str(v.value) if isinstance(v, enum.Enum) else v for v in values]
    listed = ", ".join(f"'{v}'" for v in items)
    sql = f"{column} IN ({listed})"
    return f"{column} IS NULL OR {sql}" if nullable else sql


def _check(table: str, column: str, values: Any, nullable: bool = False) -> CheckConstraint:
    return CheckConstraint(_in(column, values, nullable), name=f"{table}_{column}_check")


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))


def _ts(nullable: bool = False, default: bool = True) -> Mapped[datetime]:
    return mapped_column(
        TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=text("now()") if default else None,
    )


def _money(nullable: bool = True) -> Mapped[Decimal]:
    return mapped_column(Numeric(12, 6), nullable=nullable)


def _jsonb(default: str | None = None, nullable: bool = True) -> Mapped[Any]:
    return mapped_column(
        JSONB, nullable=nullable, server_default=text(f"'{default}'::jsonb") if default else None
    )


def _fk(target: str, nullable: bool = True, **kw: Any) -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, **kw), nullable=nullable)


class DefinitionRow(Base):
    __tablename__ = "definitions"
    sha: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    loaded_at: Mapped[datetime] = _ts()
    content: Mapped[Any] = _jsonb(nullable=False)
    errors: Mapped[Any] = _jsonb("[]", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    __table_args__ = (
        Index("definitions_one_active", "active", unique=True, postgresql_where=text("active")),
    )


class App(Base):
    __tablename__ = "apps"
    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(Text, unique=True)
    repo_kind: Mapped[str] = mapped_column(Text)
    repo_url: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(Text, server_default="main")
    created_at: Mapped[datetime] = _ts()
    archived_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    __table_args__ = (_check("apps", "repo_kind", REPO_KINDS),)


class Backlog(Base):
    __tablename__ = "backlogs"
    id: Mapped[uuid.UUID] = _pk()
    # nullable: the plan's status-check test inserts a null app_id and expects the CHECK to fire
    app_id: Mapped[uuid.UUID | None] = _fk("app.apps.id")
    seq: Mapped[int] = mapped_column(Integer)
    requirements_job_id: Mapped[uuid.UUID | None] = _fk(
        "app.jobs.id", use_alter=True, name="backlogs_requirements_job_id_fkey"
    )
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=BacklogStatus.DRAFTING.value)
    created_by: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    __table_args__ = (
        UniqueConstraint("app_id", "seq", name="backlogs_app_id_seq_key"),
        _check("backlogs", "status", BacklogStatus),
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = _pk()
    app_id: Mapped[uuid.UUID | None] = _fk("app.apps.id")  # null only for improver jobs
    seq: Mapped[int] = mapped_column(Integer)
    slug: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    feature: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, server_default=JobKind.NORMAL.value)
    source: Mapped[str] = mapped_column(Text, server_default="cli")
    source_ref: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text)
    definition_sha: Mapped[str | None] = mapped_column(Text)
    graph_version: Mapped[str | None] = mapped_column(Text)
    profile_name: Mapped[str | None] = mapped_column(Text)
    triage: Mapped[Any | None] = _jsonb()
    branch: Mapped[str | None] = mapped_column(Text)
    base_sha: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text, server_default=Stage.QUEUED.value)
    round: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_rounds: Mapped[int] = mapped_column(Integer, server_default=text("2"))
    decision: Mapped[str | None] = mapped_column(Text)
    gate_reason: Mapped[str | None] = mapped_column(Text)
    backlog_id: Mapped[uuid.UUID | None] = _fk("app.backlogs.id")
    backlog_pos: Mapped[int | None] = mapped_column(Integer)
    backlog_skipped: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    pr_url: Mapped[str | None] = mapped_column(Text)
    budget_usd: Mapped[Decimal] = _money(nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_epoch: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lease_until: Mapped[datetime | None] = _ts(nullable=True, default=False)
    crash_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    retry_after: Mapped[datetime | None] = _ts(nullable=True, default=False)
    created_at: Mapped[datetime] = _ts()
    started_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    updated_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("app_id", "seq", name="jobs_app_id_seq_key"),
        UniqueConstraint("backlog_id", "backlog_pos", name="jobs_backlog_id_backlog_pos_key"),
        _check("jobs", "kind", JobKind),
        _check("jobs", "source", JOB_SOURCES),
        _check("jobs", "stage", Stage),
        _check("jobs", "decision", Decision, nullable=True),
        CheckConstraint("app_id IS NOT NULL OR kind = 'improver'", name="jobs_app_id_check"),
        Index("jobs_stage_created_at_idx", "stage", "created_at"),
    )


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    xid: Mapped[Any] = mapped_column(XID8, server_default=text("pg_current_xact_id()"))
    job_id: Mapped[uuid.UUID | None] = _fk("app.jobs.id")
    at: Mapped[datetime] = _ts()
    kind: Mapped[str] = mapped_column(Text)
    data: Mapped[Any] = _jsonb("{}", nullable=False)
    __table_args__ = (
        Index("job_events_job_id_id_idx", "job_id", "id"),
        Index("job_events_xid_id_idx", "xid", "id"),
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = _fk("app.jobs.id", nullable=False)
    role: Mapped[str] = mapped_column(Text)
    round: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    head_sha: Mapped[str | None] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(Text)
    engine_version: Mapped[str | None] = mapped_column(Text)
    runner_digest: Mapped[str | None] = mapped_column(Text)
    model_alias: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    requested_model: Mapped[str | None] = mapped_column(Text)
    used_models: Mapped[Any] = _jsonb("[]", nullable=False)
    fallback_detected: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    selection_mode: Mapped[str | None] = mapped_column(Text)
    decision_id: Mapped[uuid.UUID | None] = _fk(
        "app.model_decisions.id", use_alter=True, name="agent_runs_decision_id_fkey"
    )
    status: Mapped[str] = mapped_column(Text, server_default=RunStatus.RUNNING.value)
    verdict: Mapped[str | None] = mapped_column(Text)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    cost_source: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[Any] = _jsonb("{}", nullable=False)
    __table_args__ = (
        UniqueConstraint("job_id", "role", "round", "attempt", name="agent_runs_attempt_key"),
        _check("agent_runs", "status", RunStatus),
        _check("agent_runs", "verdict", Verdict, nullable=True),
        _check("agent_runs", "cost_source", CostSource, nullable=True),
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = _fk("app.jobs.id", nullable=False)
    path: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    storage_uri: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = _fk("app.agent_runs.id")
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (
        _check("artifacts", "kind", ARTIFACT_KINDS),
        CheckConstraint(
            "content IS NULL OR length(content) <= 1048576", name="artifacts_content_size"
        ),
        Index("artifacts_job_id_path_created_at_idx", "job_id", "path", "created_at"),
    )


class CatalogModel(Base):
    __tablename__ = "models"
    alias: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    upstream_id: Mapped[str] = mapped_column(Text)
    strength: Mapped[str | None] = mapped_column(Text)
    price_in_per_mtok: Mapped[Decimal | None] = _money()
    price_out_per_mtok: Mapped[Decimal | None] = _money()
    context_window: Mapped[int | None] = mapped_column(Integer)
    supports_tools: Mapped[bool | None] = mapped_column(Boolean)
    messages_tools_ok: Mapped[bool | None] = mapped_column(Boolean)
    available: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    error_class: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_probe_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class ModelDecision(Base):
    __tablename__ = "model_decisions"
    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID | None] = _fk("app.jobs.id")
    run_id: Mapped[uuid.UUID | None] = _fk("app.agent_runs.id")
    role: Mapped[str | None] = mapped_column(Text)
    round: Mapped[int | None] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(Text, server_default="model")
    mode: Mapped[str] = mapped_column(Text)
    options: Mapped[Any] = _jsonb("[]", nullable=False)
    answer: Mapped[Any | None] = _jsonb()
    chosen: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    router_version: Mapped[str | None] = mapped_column(Text)
    head_version: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[Any | None] = _jsonb()
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (_check("model_decisions", "purpose", DECISION_PURPOSES),)


class Score(Base):
    __tablename__ = "scores"
    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = _fk("app.jobs.id", nullable=False)
    run_id: Mapped[uuid.UUID | None] = _fk("app.agent_runs.id")
    scorer: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    score: Mapped[Decimal | None] = mapped_column(Numeric)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)
    judge_model: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    created_at: Mapped[datetime] = _ts()


class BenchRun(Base):
    __tablename__ = "bench_runs"
    id: Mapped[uuid.UUID] = _pk()
    suite: Mapped[str] = mapped_column(Text)
    configurations: Mapped[Any] = _jsonb("[]", nullable=False)
    definition_sha: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    budget_usd: Mapped[Decimal | None] = _money()
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    __table_args__ = (_check("bench_runs", "status", BENCH_STATUSES),)


class BenchResult(Base):
    __tablename__ = "bench_results"
    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = _fk("app.bench_runs.id", nullable=False)
    task: Mapped[str] = mapped_column(Text)
    configuration: Mapped[str] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = _fk("app.jobs.id")
    hidden_passed: Mapped[bool | None] = mapped_column(Boolean)
    judge_pass: Mapped[bool | None] = mapped_column(Boolean)
    rounds: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    duration_s: Mapped[Decimal | None] = mapped_column(Numeric)


class Improvement(Base):
    __tablename__ = "improvements"
    id: Mapped[uuid.UUID] = _pk()
    trigger: Mapped[str] = mapped_column(Text)
    role_group: Mapped[str] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(Text)
    bench_run_before: Mapped[uuid.UUID | None] = _fk("app.bench_runs.id")
    bench_run_after: Mapped[uuid.UUID | None] = _fk("app.bench_runs.id")
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    created_at: Mapped[datetime] = _ts()


class Command(Base):
    __tablename__ = "commands"
    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = _fk("app.jobs.id", nullable=False)
    action: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text)
    head_sha: Mapped[str | None] = mapped_column(Text)
    gate_event_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _ts()
    consumed_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    result: Mapped[Any | None] = _jsonb()
    __table_args__ = (
        _check("commands", "action", HumanAction),
        Index("commands_job_id_created_at_idx", "job_id", "created_at"),
    )


class User(Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(Text, primary_key=True)
    role: Mapped[str] = mapped_column(Text, server_default="viewer")
    __table_args__ = (_check("users", "role", USER_ROLES),)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _ts()
    last_used_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    expires_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    revoked_at: Mapped[datetime | None] = _ts(nullable=True, default=False)


class BudgetReservation(Base):
    __tablename__ = "budget_reservations"
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = _fk("app.jobs.id", nullable=False)
    app_id: Mapped[uuid.UUID | None] = _fk("app.apps.id")
    role: Mapped[str] = mapped_column(Text)
    round: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    day: Mapped[date] = mapped_column(Date)
    month: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = _money(nullable=False)
    actual_usd: Mapped[Decimal | None] = _money()
    created_at: Mapped[datetime] = _ts()
    released_at: Mapped[datetime | None] = _ts(nullable=True, default=False)
    __table_args__ = (
        Index("budget_reservations_job_id_idx", "job_id"),
        Index("budget_reservations_open_idx", "day", postgresql_where=text("released_at IS NULL")),
    )
