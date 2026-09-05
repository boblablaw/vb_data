"""add play-by-play events + derived setter stats + contest venue/attendance

Revision ID: 0016_pbp_events
Revises: 0015_ncaa_game_id
Create Date: 2026-09-05

The play-by-play page (/contests/<id>/play_by_play) carries the full touch-by-touch rally
sequence plus substitutions. ``pbp_events`` stores one row per touch/sub/terminal (see
``vb.models.PbpEvent`` / ``vb.scrape.pbp``); ``player_pbp_stats`` holds the per-player/season
advanced stats it enables (total set attempts, assist %, setter hitting %, points played),
derived by ``vb derive-pbp``. Venue + attendance parsed from the same page land on ``contests``.
Both stat tables are read-only from the app; ``ALTER DEFAULT PRIVILEGES`` auto-grants SELECT to
``vb_app`` (no manual GRANT — see deploy/OCI_SETUP.md).
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_pbp_events"
down_revision = "0015_ncaa_game_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contests", sa.Column("location", sa.String(), nullable=True))
    op.add_column("contests", sa.Column("attendance", sa.Integer(), nullable=True))

    op.create_table(
        "pbp_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contest_id", sa.String(),
                  sa.ForeignKey("contests.contest_id", ondelete="CASCADE"), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("set_number", sa.Integer(), nullable=False),
        sa.Column("rally_number", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("touch_type", sa.String(), nullable=False),
        sa.Column("player_name", sa.String(), nullable=True),
        sa.Column("player_id", sa.Integer(),
                  sa.ForeignKey("players.id", ondelete="SET NULL"), nullable=True),
        sa.Column("team_id", sa.Integer(),
                  sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=True),
        sa.Column("is_terminal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("terminal_type", sa.String(), nullable=True),
        sa.Column("scoring_team_id", sa.Integer(), nullable=True),
        sa.Column("away_score", sa.Integer(), nullable=True),
        sa.Column("home_score", sa.Integer(), nullable=True),
        sa.UniqueConstraint("contest_id", "set_number", "seq", name="uq_pbp_event"),
    )
    op.create_index("ix_pbp_events_contest_id", "pbp_events", ["contest_id"])
    op.create_index("ix_pbp_events_season", "pbp_events", ["season"])
    op.create_index("ix_pbp_events_player_id", "pbp_events", ["player_id"])
    op.create_index("ix_pbp_events_team_id", "pbp_events", ["team_id"])

    op.create_table(
        "player_pbp_stats",
        sa.Column("player_id", sa.Integer(),
                  sa.ForeignKey("players.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("season", sa.Integer(), primary_key=True),
        sa.Column("set_attempts", sa.Integer(), nullable=True),
        sa.Column("assist_pct", sa.Float(), nullable=True),
        sa.Column("setter_hit_kills", sa.Integer(), nullable=True),
        sa.Column("setter_hit_errors", sa.Integer(), nullable=True),
        sa.Column("setter_hit_attacks", sa.Integer(), nullable=True),
        sa.Column("setter_hitting_pct", sa.Float(), nullable=True),
        sa.Column("points_played", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("player_pbp_stats")
    op.drop_index("ix_pbp_events_team_id", table_name="pbp_events")
    op.drop_index("ix_pbp_events_player_id", table_name="pbp_events")
    op.drop_index("ix_pbp_events_season", table_name="pbp_events")
    op.drop_index("ix_pbp_events_contest_id", table_name="pbp_events")
    op.drop_table("pbp_events")
    op.drop_column("contests", "attendance")
    op.drop_column("contests", "location")
