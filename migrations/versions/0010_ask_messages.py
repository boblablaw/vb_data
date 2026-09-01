"""add ask_messages (per-user in-app Ask conversation)

Revision ID: 0010_ask_messages
Revises: 0009_team_website_stats_url
Create Date: 2026-09-01

Persists each user's in-app "Ask" conversation server-side (a single ongoing thread per user)
so it survives reloads and follows the account across devices. The API role ``vb_app`` needs an
explicit INSERT/DELETE grant on this table + its sequence (SELECT is covered by ALTER DEFAULT
PRIVILEGES); see deploy/OCI_SETUP.md.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_ask_messages"
down_revision = "0009_team_website_stats_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ask_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tools", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ask_messages_user_id", "ask_messages", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_ask_messages_user_id", table_name="ask_messages")
    op.drop_table("ask_messages")
