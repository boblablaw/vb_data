"""Pytest fixtures. Postgres-backed tests are skipped when the DB is unreachable."""
from __future__ import annotations

from collections.abc import Iterator

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

# Marker embedded in every test-created account's email so cleanup can target only our rows
# (and never touch real users). Register with e.g. f"alice{TEST_EMAIL_TAG}@example.com".
TEST_EMAIL_TAG = "+vbpytest"


def _purge_test_users() -> None:
    """Delete every account whose email carries the pytest tag, plus their child rows.

    FKs from favorites / passkeys / email_verifications are ON DELETE (see the migration), but we
    delete children explicitly first to stay independent of the exact FK action.
    """
    with engine.begin() as c:
        ids = [
            r[0] for r in c.execute(
                text("SELECT id FROM users WHERE email LIKE :pat"),
                {"pat": f"%{TEST_EMAIL_TAG}%"},
            )
        ]
        if not ids:
            return
        for tbl in ("favorites", "passkey_credentials", "email_verifications"):
            c.execute(
                text(f"DELETE FROM {tbl} WHERE user_id = ANY(:ids)"), {"ids": ids}
            )
        c.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": ids})


@pytest.fixture(scope="session")
def client() -> Iterator[object]:
    """A TestClient with the app lifespan running (admin bootstrap + MCP session manager).

    Session-scoped: the mounted MCP ``StreamableHTTPSessionManager.run()`` may only start once per
    process, so the lifespan (and thus the client) is created a single time and shared. Per-test
    isolation of accounts is handled by the autouse ``_clean_test_users`` fixture below.
    """
    from fastapi.testclient import TestClient

    from vb.api.main import app

    _purge_test_users()
    with TestClient(app) as c:
        yield c
    _purge_test_users()


@pytest.fixture(autouse=True)
def _clean_test_users() -> Iterator[None]:
    """Purge tagged accounts around every test so the shared session-scoped client stays isolated.

    No-op when the DB is unreachable (the tests that create users are ``requires_db``-skipped).
    """
    if DB_AVAILABLE:
        _purge_test_users()
    yield
    if DB_AVAILABLE:
        _purge_test_users()
