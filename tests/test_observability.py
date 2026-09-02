"""Observability wiring: Sentry init is inert without a DSN, and the app still boots + serves.

These guard against a broken Sentry init path regressing the app in local dev / CI (where
``SENTRY_DSN`` is blank). No test contacts the real Sentry service.
"""
from __future__ import annotations

from conftest import requires_db


def test_init_sentry_is_noop_without_dsn(monkeypatch):
    """With a blank DSN, _init_sentry must return quietly and not call sentry_sdk.init."""
    import sentry_sdk

    from vb.api import main

    monkeypatch.setattr(main.settings, "sentry_dsn", "", raising=False)

    called = {"init": False}
    monkeypatch.setattr(sentry_sdk, "init", lambda *a, **k: called.__setitem__("init", True))

    main._init_sentry()
    assert called["init"] is False


@requires_db
def test_health_ok_with_sentry_disabled(client):
    """The health check still returns ok with Sentry off (blank DSN in the test env)."""
    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}
