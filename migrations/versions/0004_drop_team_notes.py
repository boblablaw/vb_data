"""drop teams.notes

Revision ID: 0004_drop_team_notes
Revises: 0003_coach_ncaa_fields
Create Date: 2026-09-01

The teams.json ``notes`` field (subjective campus blurbs) was loaded into ``teams.notes`` but never
served by the API or shown in the UI. The field has been removed from teams.json and the loader, so
drop the now-dead column. Downgrade re-adds it (empty).
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_drop_team_notes"
down_revision = "0003_coach_ncaa_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("teams", "notes")


def downgrade() -> None:
    op.add_column("teams", sa.Column("notes", sa.Text(), nullable=True))
