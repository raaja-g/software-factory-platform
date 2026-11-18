"""jobs.crash_count and jobs.retry_after: poison-job handling

Revision ID: 0004_job_crashes
Revises: 0003_command_gate
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_job_crashes"
down_revision = "0003_command_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("crash_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema="app",
    )
    op.add_column(
        "jobs", sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True), schema="app"
    )


def downgrade() -> None:
    op.drop_column("jobs", "retry_after", schema="app")
    op.drop_column("jobs", "crash_count", schema="app")
