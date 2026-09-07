"""add broadcasts.channel_no (TPS event-feed slot, e.g. "45" for "ESPN+ 45")

Revision ID: 0022_broadcast_channel_no
Revises: 0021_broadcasts
Create Date: 2026-09-07

The TPS playlist names event feeds like ``ESPN+ 45: Indiana vs Southern Indiana``; the ``45`` is the
"which feed to tune to" slot. We capture it so the scoreboard can show it in the tooltip for upcoming
games (it's ephemeral — TPS renumbers slots daily). Nullable; only playlist-sourced rows set it.
"""
import sqlalchemy as sa
from alembic import op

revision = "0022_broadcast_channel_no"
down_revision = "0021_broadcasts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("broadcasts", sa.Column("channel_no", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("broadcasts", "channel_no")
