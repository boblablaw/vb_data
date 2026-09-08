"""add logo path to conferences

Revision ID: 0023_conference_logo
Revises: 0022_broadcast_channel_no
Create Date: 2026-09-08

Adds ``conferences.logo`` — a static-relative path (e.g.
"assets/logos/conferences/big_ten.svg") for the conference's mark, served under /ui. The set is
curated (data/conference_logos.json, sourced from Wikipedia via ``vb scrape conference-logos``) and
copied into this column by ``vb enrich conference-logos``. Null when a conference has no sourced
logo; the UI falls back to a colored badge.
"""
import sqlalchemy as sa
from alembic import op

revision = "0023_conference_logo"
down_revision = "0022_broadcast_channel_no"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conferences", sa.Column("logo", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("conferences", "logo")
