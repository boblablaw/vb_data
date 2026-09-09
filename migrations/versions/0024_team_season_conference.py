"""add per-season conference to team_season_ids

Revision ID: 0024_team_season_conference
Revises: 0023_conference_logo
Create Date: 2026-09-09

Adds ``team_season_ids.conference_id`` — a team's conference *for that season*. Conference
membership changes year to year (realignment), so ``teams.conference_id`` (a single global value) is
only the current default; historical seasons need the season's actual affiliation. NULL until the
season's membership is loaded (``vb load-season-conferences``, sourced from
``fetch_conference_membership`` off stats.ncaa.org); reads coalesce to ``teams.conference_id`` in
that case.
"""
import sqlalchemy as sa
from alembic import op

revision = "0024_team_season_conference"
down_revision = "0023_conference_logo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "team_season_ids",
        sa.Column("conference_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_team_season_ids_conference_id",
        "team_season_ids",
        "conferences",
        ["conference_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_team_season_ids_conference_id", "team_season_ids", type_="foreignkey")
    op.drop_column("team_season_ids", "conference_id")
