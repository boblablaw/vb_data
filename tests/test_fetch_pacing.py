"""Unit tests for gentle pacing + residential-proxy config (no network, no browser).

Covers the egress-isolation / anti-flag layer added to ``ncaa_fetch``: the residential-proxy
``proxy=`` dict, the hard inter-request rate floor, and the periodic session break. ``time`` is
faked so nothing actually sleeps.
"""
from __future__ import annotations

import types

import vb.fetch.ncaa_fetch as nf


def _fake_time(sleeps: list, clock: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        sleep=lambda s: sleeps.append(s),
        monotonic=lambda: clock["t"],
    )


# --- proxy config ---------------------------------------------------------------------------------

def test_proxy_settings_none_when_unset(monkeypatch):
    monkeypatch.setattr(nf, "PROXY_URL", None)
    assert nf._proxy_settings() is None


def test_proxy_settings_full_credentials(monkeypatch):
    monkeypatch.setattr(nf, "PROXY_URL", "http://gate.provider.com:7000")
    monkeypatch.setattr(nf, "PROXY_USERNAME", "user")
    monkeypatch.setattr(nf, "PROXY_PASSWORD", "pass")
    assert nf._proxy_settings() == {
        "server": "http://gate.provider.com:7000",
        "username": "user",
        "password": "pass",
    }


def test_proxy_settings_server_only(monkeypatch):
    # A proxy URL with no separate creds (e.g. creds baked into the URL) omits username/password.
    monkeypatch.setattr(nf, "PROXY_URL", "http://gate.provider.com:7000")
    monkeypatch.setattr(nf, "PROXY_USERNAME", None)
    monkeypatch.setattr(nf, "PROXY_PASSWORD", None)
    assert nf._proxy_settings() == {"server": "http://gate.provider.com:7000"}


# --- resource blocking ----------------------------------------------------------------------------

def _fake_route(resource_type: str) -> types.SimpleNamespace:
    calls: list = []
    return types.SimpleNamespace(
        request=types.SimpleNamespace(resource_type=resource_type),
        abort=lambda: calls.append("abort"),
        continue_=lambda: calls.append("continue"),
        calls=calls,
    )


def test_route_filter_aborts_fonts_and_media(monkeypatch):
    monkeypatch.setattr(nf, "_allow_images", False)
    for rt in ("font", "media"):
        r = _fake_route(rt)
        nf._route_filter(r)
        assert r.calls == ["abort"], rt


def test_route_filter_allows_document_script_xhr(monkeypatch):
    # Akamai's challenge runs in JS: documents, scripts, styles, and XHR must always pass through.
    monkeypatch.setattr(nf, "_allow_images", False)
    for rt in ("document", "script", "stylesheet", "xhr", "fetch"):
        r = _fake_route(rt)
        nf._route_filter(r)
        assert r.calls == ["continue"], rt


def test_route_filter_images_blocked_unless_allowed(monkeypatch):
    monkeypatch.setattr(nf, "_allow_images", False)
    r = _fake_route("image")
    nf._route_filter(r)
    assert r.calls == ["abort"]

    # Headshot scrape path re-enables images.
    monkeypatch.setattr(nf, "_allow_images", True)
    r = _fake_route("image")
    nf._route_filter(r)
    assert r.calls == ["continue"]


# --- rate floor -----------------------------------------------------------------------------------

def test_request_min_interval_enforced(monkeypatch):
    sleeps: list = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(nf, "time", _fake_time(sleeps, clock))
    monkeypatch.setattr(nf.random, "uniform", lambda a, b: 0.0)  # base delay -> 0
    monkeypatch.setattr(nf, "HUMAN_DELAY_RANGE", (0.0, 0.0))
    monkeypatch.setattr(nf, "REQUEST_MIN_INTERVAL", 8.0)
    monkeypatch.setattr(nf, "PAGES_PER_BREAK", 0)
    monkeypatch.setattr(nf, "_last_fetch", 0.0)
    monkeypatch.setattr(nf, "_fetch_count", 0)

    # First fetch: no prior timestamp => the floor does not apply, nothing to sleep.
    nf.human_pause()
    assert sleeps == []

    # 2s later: must sleep the remaining 6s to honor the 8s floor since the last fetch.
    clock["t"] += 2.0
    nf.human_pause()
    assert sleeps == [6.0]

    # Well past the floor: base delay (0) governs, no extra sleep.
    clock["t"] += 100.0
    nf.human_pause()
    assert sleeps == [6.0]


def test_min_interval_disabled_by_default(monkeypatch):
    sleeps: list = []
    clock = {"t": 0.0}
    monkeypatch.setattr(nf, "time", _fake_time(sleeps, clock))
    monkeypatch.setattr(nf.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(nf, "HUMAN_DELAY_RANGE", (0.0, 0.0))
    monkeypatch.setattr(nf, "REQUEST_MIN_INTERVAL", 0.0)  # disabled
    monkeypatch.setattr(nf, "PAGES_PER_BREAK", 0)
    monkeypatch.setattr(nf, "_last_fetch", 0.0)
    monkeypatch.setattr(nf, "_fetch_count", 0)

    nf.human_pause()
    nf.human_pause()
    assert sleeps == []


# --- session break --------------------------------------------------------------------------------

def test_session_break_every_n_pages(monkeypatch):
    sleeps: list = []
    clock = {"t": 0.0}
    monkeypatch.setattr(nf, "time", _fake_time(sleeps, clock))
    # uniform(a, b) -> a: base delay 0 (no sleep), break -> the low end of BREAK_RANGE.
    monkeypatch.setattr(nf.random, "uniform", lambda a, b: a)
    monkeypatch.setattr(nf, "HUMAN_DELAY_RANGE", (0.0, 0.0))
    monkeypatch.setattr(nf, "REQUEST_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(nf, "PAGES_PER_BREAK", 3)
    monkeypatch.setattr(nf, "BREAK_RANGE", (45.0, 45.0))
    monkeypatch.setattr(nf, "_last_fetch", 0.0)
    monkeypatch.setattr(nf, "_fetch_count", 0)

    for _ in range(6):
        nf.human_pause()
    # A break fires on page 3 and page 6 (every 3rd), nowhere else.
    assert sleeps == [45.0, 45.0]


def test_no_session_break_when_disabled(monkeypatch):
    sleeps: list = []
    clock = {"t": 0.0}
    monkeypatch.setattr(nf, "time", _fake_time(sleeps, clock))
    monkeypatch.setattr(nf.random, "uniform", lambda a, b: a)
    monkeypatch.setattr(nf, "HUMAN_DELAY_RANGE", (0.0, 0.0))
    monkeypatch.setattr(nf, "REQUEST_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(nf, "PAGES_PER_BREAK", 0)  # disabled
    monkeypatch.setattr(nf, "_last_fetch", 0.0)
    monkeypatch.setattr(nf, "_fetch_count", 0)

    for _ in range(10):
        nf.human_pause()
    assert sleeps == []
