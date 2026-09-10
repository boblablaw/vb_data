"""authoritative per-set starters from ncaa.com

Revision ID: 0026_contest_set_starters
Revises: 0025_favorite_season
Create Date: 2026-09-09

Adds ``contest_set_starters`` — the six rotation starters each team fielded per set, taken verbatim
from ncaa.com's play-by-play ("Team starters: A, B, ...") via the self-hosted henrygd/ncaa-api
sidecar. When rows exist for a contest, ``query.per_set_lineups`` uses them instead of reconstructing
starters heuristically from the ``pbp_events`` sub log. Loaded by ``vb load-ncaa-lineups``.
"""
import sqlalchemy as sa
from alembic import op

revision = "0026_contest_set_starters"
down_revision = "0025_favorite_season"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contest_set_starters",
        sa.Column("contest_id", sa.String(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("set_number", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["contest_id"], ["contests.contest_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("contest_id", "team_id", "set_number", "player_id"),
    )
    op.create_index(
        "ix_contest_set_starters_season", "contest_set_starters", ["season"]
    )


def downgrade() -> None:
    op.drop_index("ix_contest_set_starters_season", table_name="contest_set_starters")
    op.drop_table("contest_set_starters")
