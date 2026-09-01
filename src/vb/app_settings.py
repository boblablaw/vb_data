"""Admin-managed runtime settings stored in the ``app_settings`` table.

Holds two secrets set via the admin panel: the single Anthropic API key
(``anthropic_api_key_global``) used by the in-app Ask box, and the MCP access token (``mcp_token``)
that gates the MCP server. Values are never returned to clients — callers expose only ``has_*``
booleans.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AppSetting

KEY_ANTHROPIC = "anthropic_api_key_global"
KEY_MCP_TOKEN = "mcp_token"


def get_setting(db: Session, key: str) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row else None


def set_setting(db: Session, key: str, value: str | None) -> None:
    """Set (or clear, when value is falsy) a setting. Caller commits."""
    row = db.get(AppSetting, key)
    if value:
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    elif row is not None:
        db.delete(row)
