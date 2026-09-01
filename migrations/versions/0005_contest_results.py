"""add match results to contests

Revision ID: 0005_contest_results
Revises: 0004_drop_team_notes
Create Date: 2026-09-01

Contests previously stored only date + home/away teams — no match outcome. The individual_stats
page the scraper already downloads carries a linescore (sets won per side + per-set point totals),
which is now parsed and persisted so the app can show wins/losses, records, and streaks. Adds
``home_sets_won`` / ``away_sets_won`` (sets won; winner = the greater) and ``set_scores`` (JSONB
``{"away": [..], "home": [..]}`` per-set points). All nullable for unplayed/unparsed contests.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_contest_results"
down_revision = "0004_drop_team_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contests", sa.Column("home_sets_won", sa.Integer(), nullable=True))
    op.add_column("contests", sa.Column("away_sets_won", sa.Integer(), nullable=True))
    op.add_column("contests", sa.Column("set_scores", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("contests", "set_scores")
    op.drop_column("contests", "away_sets_won")
    op.drop_column("contests", "home_sets_won")
