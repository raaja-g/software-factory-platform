"""laya_heads: fitted router heads, versioned per question

Revision ID: m11_01_laya_heads
Revises: m10_05_bench_task_checks
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m11_01_laya_heads"
down_revision = "m10_05_bench_task_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "laya_heads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column(
            "label_counts",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("questions_sha256", sa.Text(), nullable=False),
        sa.Column("csv_sha256", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("router_version", sa.Text()),
        sa.Column(
            "metrics", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("head_sha256", sa.Text(), nullable=False),
        sa.Column(
            "train_refs",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("evaluation", postgresql.JSONB()),
        sa.Column("activated_by", sa.Text()),
        sa.Column("activated_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("question", "version", name="laya_heads_question_version_key"),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("laya_heads", schema="app")
