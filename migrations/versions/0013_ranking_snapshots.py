"""add ranking_snapshots table (per-date RPI/AVCA history)

Revision ID: 0013_ranking_snapshots
Revises: 0012_team_avca_rank
Create Date: 2026-09-02

``teams.rpi_rank``/``avca_rank`` only hold the *current* ranking (overwritten every enrichment
run), so there is no way to know what a team was ranked on a past date. This table is the history:
one row per (season, as_of, team) copying the team's rank as it stood that day, written by
``vb snapshot-rankings`` right after ``vb enrich rpi``/``avca``. It powers "quality wins" — beating
a team that was ranked *at the time of the game* — by joining ``as_of <= contest game_date`` and
taking the most recent snapshot. History only starts from the first snapshot. The API role
``vb_app`` needs only SELECT here, which ``ALTER DEFAULT PRIVILEGES`` already auto-grants — no
manual GRANT (see deploy/OCI_SETUP.md).
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_ranking_snapshots"
down_revision = "0012_team_avca_rank"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ranking_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("team_id", sa.Integer(),
                  sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rpi_rank", sa.Integer(), nullable=True),
        sa.Column("rpi_record", sa.String(), nullable=True),
        sa.Column("avca_rank", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("season", "as_of", "team_id", name="uq_ranking_snapshot"),
    )
    op.create_index("ix_ranking_snapshots_season", "ranking_snapshots", ["season"])
    op.create_index("ix_ranking_snapshots_as_of", "ranking_snapshots", ["as_of"])
    op.create_index("ix_ranking_snapshots_team_id", "ranking_snapshots", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_ranking_snapshots_team_id", table_name="ranking_snapshots")
    op.drop_index("ix_ranking_snapshots_as_of", table_name="ranking_snapshots")
    op.drop_index("ix_ranking_snapshots_season", table_name="ranking_snapshots")
    op.drop_table("ranking_snapshots")
