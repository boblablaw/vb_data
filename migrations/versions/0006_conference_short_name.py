"""add editable short_name to conferences

Revision ID: 0006_conference_short_name
Revises: 0005_contest_results
Create Date: 2026-09-01

Conference abbreviations used to live in a hardcoded front-end map. This moves them into an
editable ``conferences.short_name`` column so they can be curated in the DB. Only conferences whose
common short form differs from the plain trimmed name are seeded (e.g. "SEC", "MAC", "A-10"); the
rest stay null and the UI falls back to the trimmed name. load-teams never writes this column, so
manual edits persist across reloads.
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_conference_short_name"
down_revision = "0005_contest_results"
branch_labels = None
depends_on = None

# Seed values: conference name -> abbreviation (only where it differs from the trimmed name).
SEED = {
    "American Conference": "AAC",
    "Atlantic 10 Conference": "A-10",
    "Atlantic Coast Conference": "ACC",
    "Atlantic Sun Conference": "ASUN",
    "Coastal Athletic Association": "CAA",
    "Conference USA": "C-USA",
    "Metro Atlantic Athletic Conference": "MAAC",
    "Mid-American Conference": "MAC",
    "Mid-Eastern Athletic Conference": "MEAC",
    "Missouri Valley Conference": "MVC",
    "Mountain West Conference": "MW",
    "Ohio Valley Conference": "OVC",
    "Southeastern Conference": "SEC",
    "Southern Conference": "SoCon",
    "Southwestern Athletic Conference": "SWAC",
    "West Coast Conference": "WCC",
    "Western Athletic Conference": "WAC",
}


def upgrade() -> None:
    op.add_column("conferences", sa.Column("short_name", sa.String(), nullable=True))
    conferences = sa.table(
        "conferences", sa.column("name", sa.String), sa.column("short_name", sa.String)
    )
    bind = op.get_bind()
    for name, abbr in SEED.items():
        bind.execute(
            conferences.update()
            .where(conferences.c.name == name)
            .values(short_name=abbr)
        )


def downgrade() -> None:
    op.drop_column("conferences", "short_name")
