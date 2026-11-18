"""commands.gate_event_id: the human.waiting event a gate command answers

Revision ID: 0003_command_gate
Revises: 0002_lease_epoch
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_command_gate"
down_revision = "0002_lease_epoch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "commands", sa.Column("gate_event_id", sa.BigInteger(), nullable=True), schema="app"
    )


def downgrade() -> None:
    op.drop_column("commands", "gate_event_id", schema="app")
