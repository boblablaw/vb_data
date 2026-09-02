"""add schedule table (per-team scheduled/played games)

Revision ID: 0011_schedule
Revises: 0010_ask_messages
Create Date: 2026-09-02

Stores each team's schedule (upcoming + played) scraped from its NCAA team page. Played detail
always comes from ``contests``; this table is the source for UPCOMING games — which have no
``contest_id`` yet — and for opponent/site labeling. One row per team perspective (a head-to-head
appears on both teams' pages), deduped on ``(season, team_id, date, opponent_name)`` for idempotent
re-loads. The API role ``vb_app`` needs only SELECT here, which ``ALTER DEFAULT PRIVILEGES`` already
auto-grants — no manual GRANT (unlike the app-write tables); see deploy/OCI_SETUP.md.
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_schedule"
down_revision = "0010_ask_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(),
                  sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opponent_team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("opponent_name", sa.String(), nullable=False),
        sa.Column("date", sa.String(), nullable=False),
        sa.Column("game_time", sa.String(), nullable=True),
        sa.Column("site", sa.String(), nullable=True),
        sa.Column("neutral_location", sa.String(), nullable=True),
        sa.Column("result_raw", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("season", "team_id", "date", "opponent_name", name="uq_schedule"),
    )
    op.create_index("ix_schedule_season", "schedule", ["season"])
    op.create_index("ix_schedule_team_id", "schedule", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_schedule_team_id", table_name="schedule")
    op.drop_index("ix_schedule_season", table_name="schedule")
    op.drop_table("schedule")
