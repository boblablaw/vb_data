"""seed short_name for the remaining conferences

Revision ID: 0007_seed_conference_short_names
Revises: 0006_conference_short_name
Create Date: 2026-09-01

0006 seeded only the 17 conferences with a distinct acronym (SEC, MAC, A-10, …). This fills every
remaining conference's short_name with its plain short form — the name minus a trailing
" Conference" ("Big Ten Conference" -> "Big Ten"; league names like "Ivy League" are unchanged) —
so the DB is the single source of truth and the front-end no longer trims names at display time.
Only null rows are touched, so the curated acronyms and any manual edits are preserved.
"""
from alembic import op

revision = "0007_seed_conference_short_names"
down_revision = "0006_conference_short_name"
branch_labels = None
depends_on = None

_TRIM = r"regexp_replace(name, '\s+Conference$', '')"


def upgrade() -> None:
    op.execute(f"UPDATE conferences SET short_name = {_TRIM} WHERE short_name IS NULL")


def downgrade() -> None:
    # Revert only the rows this migration set (short_name == the trimmed name), leaving 0006's
    # acronyms (where short_name differs from the trimmed name) intact.
    op.execute(f"UPDATE conferences SET short_name = NULL WHERE short_name = {_TRIM}")
