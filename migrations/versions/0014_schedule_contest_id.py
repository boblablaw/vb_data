"""add contest_id to schedule

Revision ID: 0014_schedule_contest_id
Revises: 0013_ranking_snapshots
Create Date: 2026-09-03

The schedule scraper now captures each row's NCAA contest id (the permanent game id, which is
both the future ``contests`` primary key and ncaa.com/game/<id>). Storing it lets an upcoming or
in-progress game link out to its NCAA page before its box score has been scraped. A single
nullable column; NULL for rows whose source exposed no contest link.
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_schedule_contest_id"
down_revision = "0013_ranking_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schedule", sa.Column("contest_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("schedule", "contest_id")
