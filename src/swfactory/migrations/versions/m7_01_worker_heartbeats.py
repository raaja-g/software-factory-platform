"""app.worker_heartbeats: one row per live worker

Revision ID: m7_01_worker_heartbeats
Revises: 0006_run_cost_nullable
"""

import sqlalchemy as sa
from alembic import op

revision = "m7_01_worker_heartbeats"
down_revision = "0006_run_cost_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.Text(), primary_key=True),
        sa.Column("at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("jobs", sa.Integer(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats", schema="app")
