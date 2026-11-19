"""app.run_requests: point-and-run requests that a worker tick turns into a requirements job

Revision ID: m7_04_run_requests
Revises: m7_03_commands_amount
"""

import sqlalchemy as sa
from alembic import op

revision = "m7_04_run_requests"
down_revision = "m7_03_commands_amount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_requests",
        sa.Column(
            "id", sa.UUID(), primary_key=True, server_default=sa.text("uuidv7()"), nullable=False
        ),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("requirement", sa.Text(), nullable=False),
        sa.Column("profile", sa.Text(), nullable=True),
        sa.Column("ci_safe", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("budget_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("app_slug", sa.Text(), nullable=True),
        sa.Column("backlog_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'waiting', 'started', 'failed')",
            name="run_requests_status_check",
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("run_requests", schema="app")
