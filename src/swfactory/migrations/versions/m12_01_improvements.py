"""improvements: job link, cycle, shas, suite, cost, evidence, error; status and trigger checks

Revision ID: m12_01_improvements
Revises: m11_02_laya_datasets
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m12_01_improvements"
down_revision = "m11_02_laya_datasets"
branch_labels = None
depends_on = None

STATUSES = (
    "queued",
    "analyzing",
    "proposing",
    "validating",
    "benchmarking",
    "pr_open",
    "branch_ready",
    "no_change",
    "rejected",
    "failed",
    "merged",
    "closed",
)
TRIGGERS = ("threshold", "age", "manual")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    t = "improvements"
    op.add_column(t, sa.Column("job_id", sa.UUID(), nullable=True), schema="app")
    op.create_foreign_key(
        "improvements_job_id_fkey",
        t,
        "jobs",
        ["job_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
    )
    op.add_column(t, sa.Column("cycle_id", sa.UUID(), nullable=True), schema="app")
    op.add_column(
        t, sa.Column("base_sha", sa.Text(), nullable=False, server_default=""), schema="app"
    )
    op.alter_column(t, "base_sha", server_default=None, schema="app")
    op.add_column(t, sa.Column("proposed_sha", sa.Text(), nullable=True), schema="app")
    op.add_column(t, sa.Column("suite", sa.Text(), nullable=False, server_default=""), schema="app")
    op.alter_column(t, "suite", server_default=None, schema="app")
    op.add_column(
        t,
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default=sa.text("0")),
        schema="app",
    )
    op.add_column(
        t,
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema="app",
    )
    op.add_column(t, sa.Column("error", sa.Text(), nullable=True), schema="app")
    op.add_column(
        t,
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="app",
    )
    op.alter_column(t, "status", server_default="queued", schema="app")
    op.execute("UPDATE app.improvements SET status = 'queued' WHERE status = 'pending'")
    op.create_check_constraint(
        "improvements_status_check", t, _in("status", STATUSES), schema="app"
    )
    op.create_check_constraint(
        "improvements_trigger_check", t, _in("trigger", TRIGGERS), schema="app"
    )


def downgrade() -> None:
    t = "improvements"
    op.drop_constraint("improvements_trigger_check", t, schema="app")
    op.drop_constraint("improvements_status_check", t, schema="app")
    op.alter_column(t, "status", server_default="pending", schema="app")
    for col in (
        "updated_at",
        "error",
        "evidence",
        "cost_usd",
        "suite",
        "proposed_sha",
        "base_sha",
        "cycle_id",
    ):
        op.drop_column(t, col, schema="app")
    op.drop_constraint("improvements_job_id_fkey", t, schema="app", type_="foreignkey")
    op.drop_column(t, "job_id", schema="app")
