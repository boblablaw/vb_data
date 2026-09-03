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


def test_browser_snippet_empty_without_dsn(monkeypatch):
    """No client-side Sentry init is injected into the UI when the DSN is blank (local dev)."""
    from vb.api import main

    monkeypatch.setattr(main.settings, "sentry_dsn", "", raising=False)
    assert main._sentry_browser_snippet() == ""


def test_browser_snippet_carries_dsn_and_release(monkeypatch):
    """When configured, the snippet initializes Sentry with the DSN, environment, and release, and
    stays errors-only (tracesSampleRate: 0) to protect the free-tier span quota."""
    from vb.api import main

    monkeypatch.setattr(main.settings, "sentry_dsn", "https://k@o1.ingest.us.sentry.io/2", raising=False)
    monkeypatch.setattr(main.settings, "sentry_environment", "production", raising=False)
    monkeypatch.setattr(main.settings, "sentry_release", "vb-data@abc1234", raising=False)

    snip = main._sentry_browser_snippet()
    assert "Sentry.init(" in snip
    assert "https://k@o1.ingest.us.sentry.io/2" in snip
    assert "vb-data@abc1234" in snip
    assert "production" in snip
    assert "tracesSampleRate: 0" in snip


def test_sentry_release_prefers_env_override(monkeypatch):
    from vb.api import main

    monkeypatch.setattr(main.settings, "sentry_release", "vb-data@deadbeef", raising=False)
    assert main._sentry_release() == "vb-data@deadbeef"

    monkeypatch.setattr(main.settings, "sentry_release", "", raising=False)
    assert main._sentry_release().startswith("vb-data@")


@requires_db
def test_health_ok_with_sentry_disabled(client):
    """The health check still returns ok with Sentry off (blank DSN in the test env)."""
    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}
