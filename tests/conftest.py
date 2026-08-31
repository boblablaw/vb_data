"""Pytest fixtures. Postgres-backed tests are skipped when the DB is unreachable."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from vb.db import engine


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()

requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason="Postgres not reachable")
