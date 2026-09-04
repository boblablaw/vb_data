"""add ncaa_game_id to contests and schedule

Revision ID: 0015_ncaa_game_id
Revises: 0014_schedule_contest_id
Create Date: 2026-09-03

ncaa.com's public game page (ncaa.com/game/<id>) uses a DIFFERENT id system from stats.ncaa.org's
``contest_id`` — the two never coincide, so ``ncaa.com/game/<contest_id>`` lands on an unrelated
game (often another sport). Store ncaa.com's own id, resolved by matching ncaa.com's scoreboard to
our games on (date + team pair), so we can link to the right public page. Nullable; NULL until
mapped by ``vb map-ncaa-games``.
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_ncaa_game_id"
down_revision = "0014_schedule_contest_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contests", sa.Column("ncaa_game_id", sa.String(), nullable=True))
    op.add_column("schedule", sa.Column("ncaa_game_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("schedule", "ncaa_game_id")
    op.drop_column("contests", "ncaa_game_id")
