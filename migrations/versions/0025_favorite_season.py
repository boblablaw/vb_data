"""make favorites season-scoped

Revision ID: 0025_favorite_season
Revises: 0024_team_season_conference
Create Date: 2026-09-09

Adds ``favorites.season`` so a favorite belongs to a single season. Team and conference ids are
stable across seasons, so the old ``(user_id, entity_type, entity_id)`` uniqueness let one favorite
show in every season; player ids are already per-season. The unique constraint now includes
``season`` so the same entity can be favorited independently per season.

Existing rows are backfilled to the current (latest) season — ``MAX(players.season)`` — per the
product decision that older seasons start empty and are re-favorited if wanted.
"""
import sqlalchemy as sa
from alembic import op

revision = "0025_favorite_season"
down_revision = "0024_team_season_conference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("favorites", sa.Column("season", sa.Integer(), nullable=True))
    # Assign every existing favorite to the current (latest) season present in the data.
    op.execute("UPDATE favorites SET season = (SELECT MAX(season) FROM players)")
    op.alter_column("favorites", "season", nullable=False)
    op.create_index("ix_favorites_season", "favorites", ["season"])
    op.drop_constraint("uq_favorite", "favorites", type_="unique")
    op.create_unique_constraint(
        "uq_favorite", "favorites", ["user_id", "entity_type", "entity_id", "season"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_favorite", "favorites", type_="unique")
    op.create_unique_constraint(
        "uq_favorite", "favorites", ["user_id", "entity_type", "entity_id"]
    )
    op.drop_index("ix_favorites_season", table_name="favorites")
    op.drop_column("favorites", "season")
