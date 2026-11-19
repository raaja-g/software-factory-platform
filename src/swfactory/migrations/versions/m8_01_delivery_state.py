"""jobs: the persisted delivery state of a GitHub job

Revision ID: m8_01_delivery_state
Revises: m7_04_run_requests
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m8_01_delivery_state"
down_revision = "m7_04_run_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("pushed_sha", sa.Text(), nullable=True), schema="app")
    op.add_column("jobs", sa.Column("merge_request_id", sa.Text(), nullable=True), schema="app")
    op.add_column(
        "jobs",
        sa.Column(
            "require_human_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        schema="app",
    )
    op.add_column(
        "jobs",
        sa.Column("follow_up_of", postgresql.UUID(as_uuid=True), nullable=True),
        schema="app",
    )
    op.create_foreign_key(
        "jobs_follow_up_of_fkey",
        "jobs",
        "jobs",
        ["follow_up_of"],
        ["id"],
        source_schema="app",
        referent_schema="app",
    )


def downgrade() -> None:
    op.drop_constraint("jobs_follow_up_of_fkey", "jobs", schema="app", type_="foreignkey")
    for col in ("follow_up_of", "require_human_approval", "merge_request_id", "pushed_sha"):
        op.drop_column("jobs", col, schema="app")
