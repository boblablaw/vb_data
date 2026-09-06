"""add ai_enabled to users

Revision ID: 0018_user_ai_enabled
Revises: 0017_pbp_serve_attempts
Create Date: 2026-09-06

Gates the in-app "Ask" AI assistant behind an admin-granted per-user flag. Off by default so no
existing account gains AI access on deploy; an admin flips it per user in the admin panel.
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_user_ai_enabled"
down_revision = "0017_pbp_serve_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "ai_enabled")
