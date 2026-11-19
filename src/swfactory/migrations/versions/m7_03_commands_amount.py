"""commands.amount_usd: the extra budget of an add_budget command

Revision ID: m7_03_commands_amount
Revises: m7_02_notifications
"""

import sqlalchemy as sa
from alembic import op

revision = "m7_03_commands_amount"
down_revision = "m7_02_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "commands", sa.Column("amount_usd", sa.Numeric(12, 6), nullable=True), schema="app"
    )
    op.create_check_constraint(
        "commands_amount_usd_check",
        "commands",
        "amount_usd IS NULL OR (action = 'add_budget' AND amount_usd > 0)",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint("commands_amount_usd_check", "commands", schema="app", type_="check")
    op.drop_column("commands", "amount_usd", schema="app")
