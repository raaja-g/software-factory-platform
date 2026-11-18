# ruff: noqa: E501
"""initial

Revision ID: 0001_initial
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


class XID8(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "xid8"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute("CREATE SCHEMA IF NOT EXISTS lg")
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
        schema="app",
    )
    op.create_table(
        "apps",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("repo_kind", sa.Text(), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), server_default="main", nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("repo_kind IN ('github', 'local')", name="apps_repo_kind_check"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        schema="app",
    )
    op.create_table(
        "bench_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("suite", sa.Text(), nullable=False),
        sa.Column(
            "configurations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("definition_sha", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("budget_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed', 'cancelled')",
            name="bench_runs_status_check",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_table(
        "definitions",
        sa.Column("sha", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "loaded_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("sha"),
        schema="app",
    )
    op.create_index(
        "definitions_one_active",
        "definitions",
        ["active"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "models",
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("upstream_id", sa.Text(), nullable=False),
        sa.Column("strength", sa.Text(), nullable=True),
        sa.Column("price_in_per_mtok", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("price_out_per_mtok", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("supports_tools", sa.Boolean(), nullable=True),
        sa.Column("messages_tools_ok", sa.Boolean(), nullable=True),
        sa.Column("available", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_probe_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("alias"),
        schema="app",
    )
    op.create_table(
        "users",
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default="viewer", nullable=False),
        sa.CheckConstraint(
            "role IN ('viewer', 'submitter', 'approver', 'admin')", name="users_role_check"
        ),
        sa.PrimaryKeyConstraint("email"),
        schema="app",
    )
    op.create_table(
        "backlogs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("requirements_job_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="drafting", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('drafting', 'awaiting_approval', 'running', 'paused', 'done', 'cancelled')",
            name="backlogs_status_check",
        ),
        sa.ForeignKeyConstraint(
            ["app_id"],
            ["app.apps.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "seq", name="backlogs_app_id_seq_key"),
        schema="app",
    )
    op.create_table(
        "improvements",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("role_group", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("bench_run_before", sa.UUID(), nullable=True),
        sa.Column("bench_run_after", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["bench_run_after"],
            ["app.bench_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["bench_run_before"],
            ["app.bench_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("feature", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="normal", nullable=False),
        sa.Column("source", sa.Text(), server_default="cli", nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("definition_sha", sa.Text(), nullable=True),
        sa.Column("graph_version", sa.Text(), nullable=True),
        sa.Column("profile_name", sa.Text(), nullable=True),
        sa.Column("triage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("stage", sa.Text(), server_default="queued", nullable=False),
        sa.Column("round", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_rounds", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("gate_reason", sa.Text(), nullable=True),
        sa.Column("backlog_id", sa.UUID(), nullable=True),
        sa.Column("backlog_pos", sa.Integer(), nullable=True),
        sa.Column("backlog_skipped", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("budget_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_until", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("app_id IS NOT NULL OR kind = 'improver'", name="jobs_app_id_check"),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('APPROVE', 'ESCALATE')", name="jobs_decision_check"
        ),
        sa.CheckConstraint(
            "kind IN ('normal', 'benchmark', 'improver', 'requirements')", name="jobs_kind_check"
        ),
        sa.CheckConstraint(
            "source IN ('dashboard', 'cli', 'github_issue', 'schedule', 'webhook', 'backlog', 'api')",
            name="jobs_source_check",
        ),
        sa.CheckConstraint(
            "stage IN ('queued', 'triage', 'spec', 'build', 'verify', 'review', 'approve', 'needs_human', 'delivering', 'delivered', 'merged', 'closed', 'failed', 'cancelled')",
            name="jobs_stage_check",
        ),
        sa.ForeignKeyConstraint(
            ["app_id"],
            ["app.apps.id"],
        ),
        sa.ForeignKeyConstraint(
            ["backlog_id"],
            ["app.backlogs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "seq", name="jobs_app_id_seq_key"),
        sa.UniqueConstraint("backlog_id", "backlog_pos", name="jobs_backlog_id_backlog_pos_key"),
        schema="app",
    )
    op.create_index(
        "jobs_stage_created_at_idx", "jobs", ["stage", "created_at"], unique=False, schema="app"
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("round", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column("engine", sa.Text(), nullable=False),
        sa.Column("engine_version", sa.Text(), nullable=True),
        sa.Column("runner_digest", sa.Text(), nullable=True),
        sa.Column("model_alias", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("requested_model", sa.Text(), nullable=True),
        sa.Column(
            "used_models",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "fallback_detected", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("selection_mode", sa.Text(), nullable=True),
        sa.Column("decision_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("verdict", sa.Text(), nullable=True),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("cost_source", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cost_source IS NULL OR cost_source IN ('gateway', 'engine', 'estimate')",
            name="agent_runs_cost_source_check",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'failed', 'timeout', 'cancelled', 'budget')",
            name="agent_runs_status_check",
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN ('PASS', 'CHANGES', 'ERROR', 'SKIPPED')",
            name="agent_runs_verdict_check",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["app.jobs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "role", "round", "attempt", name="agent_runs_attempt_key"),
        schema="app",
    )
    op.create_table(
        "bench_results",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("configuration", sa.Text(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("hidden_passed", sa.Boolean(), nullable=True),
        sa.Column("judge_pass", sa.Boolean(), nullable=True),
        sa.Column("rounds", sa.Integer(), nullable=True),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("duration_s", sa.Numeric(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["app.jobs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["app.bench_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_table(
        "commands",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("consumed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "action IN ('approve', 'rework', 'reject', 'retry', 'add_budget', 'cancel')",
            name="commands_action_check",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["app.jobs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "commands_job_id_created_at_idx",
        "commands",
        ["job_id", "created_at"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "xid",
            XID8(),
            server_default=sa.text("pg_current_xact_id()"),
            nullable=False,
        ),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column(
            "at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["app.jobs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "job_events_job_id_id_idx", "job_events", ["job_id", "id"], unique=False, schema="app"
    )
    op.create_index(
        "job_events_xid_id_idx", "job_events", ["xid", "id"], unique=False, schema="app"
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("storage_uri", sa.Text(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('md', 'log', 'transcript', 'image', 'json')", name="artifacts_kind_check"
        ),
        sa.CheckConstraint(
            "content IS NULL OR length(content) <= 1048576", name="artifacts_content_size"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["app.jobs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["app.agent_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "artifacts_job_id_path_created_at_idx",
        "artifacts",
        ["job_id", "path", "created_at"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "model_decisions",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.Column("purpose", sa.Text(), server_default="model", nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("answer", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("chosen", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("router_version", sa.Text(), nullable=True),
        sa.Column("head_version", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("purpose IN ('model', 'triage')", name="model_decisions_purpose_check"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["app.jobs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["app.agent_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_table(
        "scores",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("scorer", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("score", sa.Numeric(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("judge_model", sa.Text(), nullable=True),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["app.jobs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["app.agent_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    _post_tables()


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS app.artifacts_latest")
    op.execute("DROP FUNCTION IF EXISTS app.backlog_turn(app.jobs)")
    for t in ("backlogs",):
        op.drop_constraint("backlogs_requirements_job_id_fkey", t, schema="app", type_="foreignkey")
    op.drop_constraint(
        "agent_runs_decision_id_fkey", "agent_runs", schema="app", type_="foreignkey"
    )
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("scores", schema="app")
    op.drop_table("model_decisions", schema="app")
    op.drop_index("artifacts_job_id_path_created_at_idx", table_name="artifacts", schema="app")
    op.drop_table("artifacts", schema="app")
    op.drop_index("job_events_xid_id_idx", table_name="job_events", schema="app")
    op.drop_index("job_events_job_id_id_idx", table_name="job_events", schema="app")
    op.drop_table("job_events", schema="app")
    op.drop_index("commands_job_id_created_at_idx", table_name="commands", schema="app")
    op.drop_table("commands", schema="app")
    op.drop_table("bench_results", schema="app")
    op.drop_table("agent_runs", schema="app")
    op.drop_index("jobs_stage_created_at_idx", table_name="jobs", schema="app")
    op.drop_table("jobs", schema="app")
    op.drop_table("improvements", schema="app")
    op.drop_table("backlogs", schema="app")
    op.drop_table("users", schema="app")
    op.drop_table("models", schema="app")
    op.drop_index(
        "definitions_one_active",
        table_name="definitions",
        schema="app",
        postgresql_where=sa.text("active"),
    )
    op.drop_table("definitions", schema="app")
    op.drop_table("bench_runs", schema="app")
    op.drop_table("apps", schema="app")
    op.drop_table("api_tokens", schema="app")

    op.execute("DROP FUNCTION IF EXISTS app.notify_row() CASCADE")


def _post_tables() -> None:
    op.create_foreign_key(
        "backlogs_requirements_job_id_fkey",
        "backlogs",
        "jobs",
        ["requirements_job_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
    )
    op.create_foreign_key(
        "agent_runs_decision_id_fkey",
        "agent_runs",
        "model_decisions",
        ["decision_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
    )
    op.execute(
        "CREATE VIEW app.artifacts_latest AS "
        "SELECT DISTINCT ON (job_id, path) * FROM app.artifacts ORDER BY job_id, path, created_at DESC"
    )
    op.execute(
        """
        CREATE FUNCTION app.backlog_turn(j app.jobs) RETURNS boolean LANGUAGE sql STABLE AS $$
        SELECT j.backlog_id IS NULL OR j.backlog_pos IS NULL OR (
            NOT EXISTS (SELECT 1 FROM app.backlogs b WHERE b.id = j.backlog_id AND b.status = 'cancelled')
            AND NOT EXISTS (SELECT 1 FROM app.jobs p WHERE p.backlog_id = j.backlog_id
                            AND p.backlog_pos < j.backlog_pos AND p.stage <> 'merged' AND NOT p.backlog_skipped))
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.notify_row() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN PERFORM pg_notify(TG_ARGV[0], json_build_object('id', NEW.id, 'job_id', NEW.job_id)::text); RETURN NEW; END $$
        """
    )
    op.execute(
        "CREATE TRIGGER job_events_notify AFTER INSERT ON app.job_events "
        "FOR EACH ROW EXECUTE FUNCTION app.notify_row('job_events')"
    )
    op.execute(
        "CREATE TRIGGER commands_notify AFTER INSERT ON app.commands "
        "FOR EACH ROW EXECUTE FUNCTION app.notify_row('commands')"
    )
    # least privilege; roles are cluster-wide, so create them only if absent
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_app') THEN
                CREATE ROLE factory_app NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_graph') THEN
                CREATE ROLE factory_graph NOLOGIN;
            END IF;
        END $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA app TO factory_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO factory_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO factory_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO factory_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT USAGE, SELECT ON SEQUENCES TO factory_app"
    )
    op.execute("GRANT USAGE, CREATE ON SCHEMA lg TO factory_graph")
    op.execute("ALTER ROLE factory_graph SET search_path = lg")
    op.execute(
        """
        DO $$ BEGIN
            EXECUTE format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO factory_app, factory_graph', current_database());
        END $$
        """
    )
