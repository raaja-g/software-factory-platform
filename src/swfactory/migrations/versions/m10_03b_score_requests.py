"""app.score_requests: API-to-worker requests to score a job (judges run on the worker)

Revision ID: m10_03b_score_requests
Revises: m10_03_judge_labels
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m10_03b_score_requests"
down_revision = "m10_03_judge_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "score_requests",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("app.jobs.id"), nullable=False),
        sa.Column("scorers", postgresql.JSONB(), nullable=True),
        sa.Column("force", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="score_requests_status_check",
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("score_requests", schema="app")
