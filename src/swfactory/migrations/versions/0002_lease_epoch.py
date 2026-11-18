"""lease epoch

Revision ID: 0002_lease_epoch
Revises: 0001_initial
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_lease_epoch"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("lease_epoch", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("jobs", "lease_epoch", schema="app")
