"""bench_runs and bench_results gain the estimate, repetitions, overrides and trial status

Revision ID: m10_04_bench
Revises: m10_03b_score_requests
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m10_04_bench"
down_revision = "m10_03b_score_requests"
branch_labels = None
depends_on = None

RUN_STATUS = "status IN ('running', 'done', 'stopped_budget', 'cancelled')"
OLD_RUN_STATUS = "status IN ('pending', 'running', 'done', 'failed', 'cancelled')"
RESULT_STATUS = (
    "status IN ("
    "'pending', 'running', 'done', 'skipped_budget', 'budget_stopped', 'skipped_invalid'"
    ")"
)


def upgrade() -> None:
    op.drop_constraint("bench_runs_status_check", "bench_runs", schema="app")
    op.execute("UPDATE app.bench_runs SET status = 'running' WHERE status = 'pending'")
    op.execute("UPDATE app.bench_runs SET status = 'cancelled' WHERE status = 'failed'")
    op.alter_column("bench_runs", "status", server_default="running", schema="app")
    op.create_check_constraint("bench_runs_status_check", "bench_runs", RUN_STATUS, schema="app")
    op.add_column("bench_runs", sa.Column("estimate_usd", sa.Numeric(12, 6)), schema="app")
    op.add_column(
        "bench_runs",
        sa.Column("repetitions", sa.Integer(), server_default=sa.text("1"), nullable=False),
        schema="app",
    )
    op.add_column(
        "bench_runs",
        sa.Column("max_parallel", sa.Integer(), server_default=sa.text("1"), nullable=False),
        schema="app",
    )
    op.add_column("bench_runs", sa.Column("requested_by", sa.Text()), schema="app")

    op.add_column(
        "bench_results",
        sa.Column("repetition", sa.Integer(), server_default=sa.text("1"), nullable=False),
        schema="app",
    )
    op.add_column(
        "bench_results",
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        schema="app",
    )
    op.add_column("bench_results", sa.Column("overrides", postgresql.JSONB()), schema="app")
    op.add_column("bench_results", sa.Column("hidden_detail", sa.Text()), schema="app")
    op.add_column("bench_results", sa.Column("judge_pass_uncalibrated", sa.Boolean()), schema="app")
    op.create_check_constraint(
        "bench_results_status_check", "bench_results", RESULT_STATUS, schema="app"
    )
    op.create_unique_constraint(
        "bench_results_trial_key",
        "bench_results",
        ["run_id", "task", "configuration", "repetition"],
        schema="app",
    )
    op.create_unique_constraint(
        "bench_results_job_id_key", "bench_results", ["job_id"], schema="app"
    )


def downgrade() -> None:
    for name in ("bench_results_job_id_key", "bench_results_trial_key"):
        op.drop_constraint(name, "bench_results", schema="app", type_="unique")
    op.drop_constraint("bench_results_status_check", "bench_results", schema="app")
    for col in ("judge_pass_uncalibrated", "hidden_detail", "overrides", "status", "repetition"):
        op.drop_column("bench_results", col, schema="app")
    for col in ("requested_by", "max_parallel", "repetitions", "estimate_usd"):
        op.drop_column("bench_runs", col, schema="app")
    op.drop_constraint("bench_runs_status_check", "bench_runs", schema="app")
    op.execute("UPDATE app.bench_runs SET status = 'cancelled' WHERE status = 'stopped_budget'")
    op.alter_column("bench_runs", "status", server_default="pending", schema="app")
    op.create_check_constraint(
        "bench_runs_status_check", "bench_runs", OLD_RUN_STATUS, schema="app"
    )
