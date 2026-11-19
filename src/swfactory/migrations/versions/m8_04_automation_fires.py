"""app.automation_fires: one row per schedule slot, so a slot makes at most one job

Revision ID: m8_04_automation_fires
Revises: m8_01_delivery_state
"""

import sqlalchemy as sa
from alembic import op

revision = "m8_04_automation_fires"
down_revision = "m8_01_delivery_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_fires",
        sa.Column("automation", sa.Text(), nullable=False),
        sa.Column("fire_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("app.jobs.id"), nullable=True),
        sa.PrimaryKeyConstraint("automation", "fire_at"),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("automation_fires", schema="app")
