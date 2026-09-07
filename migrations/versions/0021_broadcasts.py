"""add broadcasts table (per-game TV/streaming networks)

Revision ID: 0021_broadcasts
Revises: 0020_pbp_attack_splits
Create Date: 2026-09-07

Network tags shown on the scoreboard game cards. Populated by ``vb ingest-broadcasts`` from public
conference ICS calendars (primary) + the personal TPS IPTV feeds (fallback), matched to our games on
(game_date + unordered team pair). See ``vb.models.Broadcast`` / ``vb.load.broadcasts``.
"""
import sqlalchemy as sa
from alembic import op

revision = "0021_broadcasts"
down_revision = "0020_pbp_attack_splits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("game_date", sa.String(), nullable=False),
        sa.Column("team_a_id", sa.Integer(),
                  sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("team_b_id", sa.Integer(),
                  sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("network", sa.String(), nullable=False),
        sa.Column("logo_key", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("is_live", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("raw_channel", sa.String(), nullable=True),
        sa.Column("start_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("season", "game_date", "team_a_id", "team_b_id", "network",
                            name="uq_broadcast"),
    )
    op.create_index("ix_broadcasts_season", "broadcasts", ["season"])
    op.create_index("ix_broadcasts_game_date", "broadcasts", ["game_date"])


def downgrade() -> None:
    op.drop_index("ix_broadcasts_game_date", table_name="broadcasts")
    op.drop_index("ix_broadcasts_season", table_name="broadcasts")
    op.drop_table("broadcasts")
