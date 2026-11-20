"""app.scores: nullable score and passed, judge calibration shas, (job_id, scorer) index

Revision ID: m10_01_scores
Revises: m8_03_github_intake
"""

import sqlalchemy as sa
from alembic import op

revision = "m10_01_scores"
down_revision = "m8_03_github_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("scores", "score", existing_type=sa.Numeric(), nullable=True, schema="app")
    op.alter_column("scores", "passed", existing_type=sa.Boolean(), nullable=True, schema="app")
    op.add_column("scores", sa.Column("rubric_sha", sa.Text(), nullable=True), schema="app")
    op.add_column("scores", sa.Column("judge_sha", sa.Text(), nullable=True), schema="app")
    op.create_index("scores_job_id_scorer_idx", "scores", ["job_id", "scorer"], schema="app")


def downgrade() -> None:
    op.drop_index("scores_job_id_scorer_idx", table_name="scores", schema="app")
    op.drop_column("scores", "judge_sha", schema="app")
    op.drop_column("scores", "rubric_sha", schema="app")
