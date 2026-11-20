"""laya_datasets and laya_imported_rows: imported synthetic training rows; laya_heads.source_counts

Revision ID: m11_02_laya_datasets
Revises: m11_01_laya_heads
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m11_02_laya_datasets"
down_revision = "m11_01_laya_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "laya_datasets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("origin_url", sa.Text(), nullable=False),
        sa.Column("licence", sa.Text(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False, unique=True),
        sa.Column("imported_by", sa.Text(), nullable=False),
        sa.Column(
            "imported_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("question", "name", name="laya_datasets_question_name_key"),
        schema="app",
    )
    op.create_table(
        "laya_imported_rows",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app.laya_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        schema="app",
    )
    op.create_index(
        "laya_imported_rows_question_idx", "laya_imported_rows", ["question"], schema="app"
    )
    op.add_column(
        "laya_heads",
        sa.Column(
            "source_counts",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("laya_heads", "source_counts", schema="app")
    op.drop_table("laya_imported_rows", schema="app")
    op.drop_table("laya_datasets", schema="app")
