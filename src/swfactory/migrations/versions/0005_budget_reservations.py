"""app.budget_reservations: run-level budget holds

Revision ID: 0005_budget_reservations
Revises: 0004_job_crashes
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_budget_reservations"
down_revision = "0004_job_crashes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_reservations",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.jobs.id"), nullable=False
        ),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.apps.id")),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("round", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 6), nullable=False),
        sa.Column("actual_usd", sa.Numeric(12, 6)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        schema="app",
    )
    op.create_index(
        "budget_reservations_job_id_idx", "budget_reservations", ["job_id"], schema="app"
    )
    op.create_index(
        "budget_reservations_open_idx",
        "budget_reservations",
        ["day"],
        schema="app",
        postgresql_where=sa.text("released_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("budget_reservations", schema="app")
