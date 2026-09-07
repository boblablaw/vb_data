"""add FBSO/transition attack splits to player_pbp_stats

Revision ID: 0020_pbp_attack_splits
Revises: 0019_weeks_from_schedule
Create Date: 2026-09-07

Per-player attack lines split by rally phase — first-ball side-out (the receiving team's first swing
off a serve reception) vs transition (every other attack) — derived from the same play-by-play
events. Six nullable counts (kills/errors/attacks per phase); null until the next ``vb derive-pbp``
run back-populates them. See ``vb.derive.pbp.attack_splits_by_player``.
"""
import sqlalchemy as sa
from alembic import op

revision = "0020_pbp_attack_splits"
down_revision = "0019_weeks_from_schedule"
branch_labels = None
depends_on = None

_COLS = (
    "fbso_kills", "fbso_errors", "fbso_attacks",
    "trans_kills", "trans_errors", "trans_attacks",
)


def upgrade() -> None:
    for col in _COLS:
        op.add_column("player_pbp_stats", sa.Column(col, sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in reversed(_COLS):
        op.drop_column("player_pbp_stats", col)
