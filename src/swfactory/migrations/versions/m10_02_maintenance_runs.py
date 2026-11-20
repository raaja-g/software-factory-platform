"""app.maintenance_runs: when each nightly maintenance task last ran

Revision ID: m10_02_maintenance_runs
Revises: m10_01_scores
"""

import sqlalchemy as sa
from alembic import op

revision = "m10_02_maintenance_runs"
down_revision = "m10_01_scores"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_runs",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name"),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("maintenance_runs", schema="app")
