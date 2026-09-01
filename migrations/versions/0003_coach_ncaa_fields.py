"""coach NCAA fields: ncaa_coach_id, seasons, record

Revision ID: 0003_coach_ncaa_fields
Revises: 0002_contest_weeks
Create Date: 2026-09-01

Head coaches are now sourced from the NCAA roster page (see load/coaches.py) instead of the
hand-maintained teams.json. The roster scrape already captures the coach's NCAA people id, tenure
("Seasons"), and career "Record"; these columns persist them. email/phone stay on the model but are
now always null (NCAA gives no contact info).
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_coach_ncaa_fields"
down_revision = "0002_contest_weeks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("coaches", sa.Column("ncaa_coach_id", sa.String(), nullable=True))
    op.add_column("coaches", sa.Column("seasons", sa.String(), nullable=True))
    op.add_column("coaches", sa.Column("record", sa.String(), nullable=True))
    op.create_index("ix_coaches_ncaa_coach_id", "coaches", ["ncaa_coach_id"])


def downgrade() -> None:
    op.drop_index("ix_coaches_ncaa_coach_id", table_name="coaches")
    op.drop_column("coaches", "record")
    op.drop_column("coaches", "seasons")
    op.drop_column("coaches", "ncaa_coach_id")
