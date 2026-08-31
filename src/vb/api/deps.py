"""Shared FastAPI dependencies."""
from __future__ import annotations

from ..db import get_session

__all__ = ["get_session"]
