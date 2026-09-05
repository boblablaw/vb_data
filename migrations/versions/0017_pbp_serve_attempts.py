"""add serve_attempts to player_pbp_stats

Revision ID: 0017_pbp_serve_attempts
Revises: 0016_pbp_events
Create Date: 2026-09-05

Serve attempts — every ``serve`` touch in the play-by-play — are a natural companion to
``set_attempts`` and come from the same events, so they live on ``player_pbp_stats``. Nullable and
back-populated by ``vb derive-pbp`` (null until the next derive run).
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_pbp_serve_attempts"
down_revision = "0016_pbp_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("player_pbp_stats", sa.Column("serve_attempts", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("player_pbp_stats", "serve_attempts")
