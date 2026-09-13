"""precomputed per-team scouting reports

Revision ID: 0027_scouting_reports
Revises: 0026_contest_set_starters
Create Date: 2026-09-13

Adds ``scouting_reports`` — one row per (team, season) holding a deterministically-built scouting
report (``data`` JSONB: league-percentile stats, roster leaders, setter system, per-rotation
strengths/weaknesses, insight/outlier callouts, and two prose sections). Built weekly by
``vb build-scouting`` (Mon morning); the API only reads it. Like ``ranking_snapshots``, the
``vb_app`` role needs only SELECT here, which ``ALTER DEFAULT PRIVILEGES`` already auto-grants — no
manual GRANT (see deploy/OCI_SETUP.md).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027_scouting_reports"
down_revision = "0026_contest_set_starters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scouting_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "season", name="uq_scouting_report"),
    )
    op.create_index("ix_scouting_reports_season", "scouting_reports", ["season"])
    op.create_index("ix_scouting_reports_team_id", "scouting_reports", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_scouting_reports_team_id", table_name="scouting_reports")
    op.drop_index("ix_scouting_reports_season", table_name="scouting_reports")
    op.drop_table("scouting_reports")
