"""add avca_rank to teams

Revision ID: 0012_team_avca_rank
Revises: 0011_schedule
Create Date: 2026-09-02

The AVCA Coaches Poll (top 25) is surfaced next to RPI across the UI — team detail, standings,
the scoreboard, and team schedules. This adds a single nullable column, populated weekly by
``vb enrich avca`` from the NCAA.com AVCA rankings table (the sibling of the RPI table). Teams
outside the top 25 keep NULL; each enrichment run clears stale ranks before setting the new poll.
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_team_avca_rank"
down_revision = "0011_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("teams", sa.Column("avca_rank", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("teams", "avca_rank")
