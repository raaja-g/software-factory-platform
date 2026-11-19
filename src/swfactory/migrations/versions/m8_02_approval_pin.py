"""jobs.head_sha and commands.source_ref: approvals pinned to a head, one row per PR comment

Revision ID: m8_02_approval_pin
Revises: m8_04_automation_fires
"""

import sqlalchemy as sa
from alembic import op

revision = "m8_02_approval_pin"
down_revision = "m8_04_automation_fires"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("head_sha", sa.Text(), nullable=True), schema="app")
    op.add_column("commands", sa.Column("source_ref", sa.Text(), nullable=True), schema="app")
    op.create_index(
        "commands_source_ref_uq",
        "commands",
        ["source_ref"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("commands_source_ref_uq", table_name="commands", schema="app")
    op.drop_column("commands", "source_ref", schema="app")
    op.drop_column("jobs", "head_sha", schema="app")
