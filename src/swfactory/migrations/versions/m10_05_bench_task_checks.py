"""bench_task_checks: whether a task's hidden checks fail at the base and pass on the merge

Revision ID: m10_05_bench_task_checks
Revises: m10_04_bench
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m10_05_bench_task_checks"
down_revision = "m10_04_bench"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bench_task_checks",
        sa.Column("check_key", sa.Text(), primary_key=True),
        sa.Column("suite", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.jobs.id")),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "detail", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'valid', 'invalid')",
            name="bench_task_checks_status_check",
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("bench_task_checks", schema="app")
