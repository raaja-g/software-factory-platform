"""app.judge_labels: your labels on judge rows, one per job, scorer and rubric

Revision ID: m10_03_judge_labels
Revises: m10_02_maintenance_runs
"""

import sqlalchemy as sa
from alembic import op

revision = "m10_03_judge_labels"
down_revision = "m10_02_maintenance_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "judge_labels",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("app.jobs.id"), nullable=False),
        sa.Column("scorer", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("rubric_sha", sa.Text(), nullable=False),
        sa.Column("labelled_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "scorer", "rubric_sha"),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("judge_labels", schema="app")
