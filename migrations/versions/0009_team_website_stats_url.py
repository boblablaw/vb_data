"""add website and stats_url to teams

Revision ID: 0009_team_website_stats_url
Revises: 0008_auth_tables
Create Date: 2026-09-01

The team detail page links out to each program's official athletics roster page and its stats
page. Those URLs already live in ``data/teams.json`` (``url`` and ``stats_url``); this adds the
columns so ``vb load-teams`` can seed them. Both are nullable and left untouched by any scraper.
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_team_website_stats_url"
down_revision = "0008_auth_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("teams", sa.Column("website", sa.String(), nullable=True))
    op.add_column("teams", sa.Column("stats_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("teams", "stats_url")
    op.drop_column("teams", "website")
