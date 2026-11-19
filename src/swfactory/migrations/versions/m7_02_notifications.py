"""app.notifications: keys of notices already sent

Revision ID: m7_02_notifications
Revises: m7_01_worker_heartbeats
"""

import sqlalchemy as sa
from alembic import op

revision = "m7_02_notifications"
down_revision = "m7_01_worker_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "sent_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("notifications", schema="app")
