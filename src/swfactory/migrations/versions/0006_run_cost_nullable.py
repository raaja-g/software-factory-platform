"""app.agent_runs.cost_usd is null until the run's cost is known

Revision ID: 0006_run_cost_nullable
Revises: 0005_budget_reservations
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_run_cost_nullable"
down_revision = "0005_budget_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "agent_runs",
        "cost_usd",
        existing_type=sa.Numeric(precision=12, scale=6),
        nullable=True,
        server_default=None,
        schema="app",
    )


def downgrade() -> None:
    op.execute("update app.agent_runs set cost_usd = 0 where cost_usd is null")
    op.alter_column(
        "agent_runs",
        "cost_usd",
        existing_type=sa.Numeric(precision=12, scale=6),
        nullable=False,
        server_default=sa.text("0"),
        schema="app",
    )
