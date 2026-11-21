"""app.web_sessions: server-side dashboard sessions (only sha256 of the cookie secret is stored)

Revision ID: m13_01_web_sessions
Revises: m12_01_improvements
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m13_01_web_sessions"
down_revision = "m12_01_improvements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("binding", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_seen_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_hash"),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("web_sessions", schema="app")
