"""GitHub intake: webhook dedupe, poll ETags and one row per issue labelling

Revision ID: m8_03_github_intake
Revises: m8_02_approval_pin
"""

import sqlalchemy as sa
from alembic import op

revision = "m8_03_github_intake"
down_revision = "m8_02_approval_pin"
branch_labels = None
depends_on = None

TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "webhook_deliveries",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("received_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("source", "delivery_id"),
        schema="app",
    )
    op.create_table(
        "github_poll_state",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("polled_at", TS, server_default=sa.text("now()"), nullable=False),
        schema="app",
    )
    op.create_table(
        "issue_intakes",
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("labeled_at", TS, nullable=False),
        sa.Column("labeled_by", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("author_permission", sa.Text(), nullable=False),
        sa.Column("labeler_permission", sa.Text(), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("app.jobs.id"), nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("reported_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("repo", "issue_number", "labeled_at"),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("issue_intakes", schema="app")
    op.drop_table("github_poll_state", schema="app")
    op.drop_table("webhook_deliveries", schema="app")
