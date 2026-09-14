"""TTL'd scoreboard-discovery cache in ``scrape.game_stats`` — the proxy-bandwidth optimization.

Scoreboard discovery is the only stats.ncaa.org (metered-proxy) fetch that repeats across a run
(game-stats then pbp) and across hourly runs (stable older dates). These tests pin the caching
behavior that collapses those redundant fetches without changing which contests are discovered.
No network: ``fetch_html`` is stubbed and counted; the cache file is redirected to a tmp path.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import vb.scrape.game_stats as gs


def _html(cids):
    return "".join(f'<a href="/contests/{c}/box_score">x</a>' for c in cids)


def _mmddyyyy(days_ago):
    d = datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=days_ago)
    return d.strftime("%m/%d/%Y")


def _patch(monkeypatch, tmp_path, htmls):
    """Stub fetch_html to return ``htmls`` on successive calls (last repeats) and count calls."""
    calls = {"n": 0}

    def fake_fetch(url, wait_selectors=None):
        i = min(calls["n"], len(htmls) - 1)
        calls["n"] += 1
        return htmls[i]

    monkeypatch.setattr(gs, "fetch_html", fake_fetch)
    monkeypatch.setattr(gs, "_scoreboard_cache_path", lambda year: tmp_path / f"sb_{year}.json")
    return calls


def test_recent_date_cached_within_ttl(monkeypatch, tmp_path):
    """The second discovery of the same date (e.g. the pbp step) reuses the cache — no re-fetch."""
    calls = _patch(monkeypatch, tmp_path, [_html(["111", "222"])])
    d = _mmddyyyy(0)  # today
    a = gs.discover_contests_by_date(d, 2026)
    b = gs.discover_contests_by_date(d, 2026)
    assert a == ["111", "222"] == b
    assert calls["n"] == 1  # only one scoreboard fetch for both discoveries


def test_fresh_scoreboard_bypasses_read_but_refreshes(monkeypatch, tmp_path):
    """``use_cache=False`` (the daily pass) ignores the cache on read yet rewrites it."""
    calls = _patch(monkeypatch, tmp_path, [_html(["1"]), _html(["1", "2"])])
    d = _mmddyyyy(0)
    gs.discover_contests_by_date(d, 2026)                       # fetch #1 -> caches ["1"]
    r = gs.discover_contests_by_date(d, 2026, use_cache=False)  # fetch #2 -> ignores cache
    assert r == ["1", "2"]
    assert calls["n"] == 2
    r2 = gs.discover_contests_by_date(d, 2026)                  # cached read sees the fresh list
    assert r2 == ["1", "2"]
    assert calls["n"] == 2                                      # no new fetch


def test_stable_date_gets_long_ttl(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, [_html(["9"])])
    d = _mmddyyyy(3)  # 3 days back -> stable
    gs.discover_contests_by_date(d, 2026)
    assert gs._read_scoreboard_cache(2026)[d]["ttl"] == gs._SCOREBOARD_TTL_STABLE


def test_recent_date_gets_short_ttl(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, [_html(["9"])])
    d = _mmddyyyy(1)  # yesterday -> recent (re-fetched hourly for the Hawaii late-finish case)
    gs.discover_contests_by_date(d, 2026)
    assert gs._read_scoreboard_cache(2026)[d]["ttl"] == gs._SCOREBOARD_TTL_RECENT


def test_empty_result_gets_short_ttl(monkeypatch, tmp_path):
    """An empty scoreboard (possibly a transient Akamai block) is only cached briefly, never long,
    even for an otherwise-'stable' older date — so a mistakenly-empty day is retried soon."""
    _patch(monkeypatch, tmp_path, [""])
    d = _mmddyyyy(3)  # would be 'stable' if it had games
    assert gs.discover_contests_by_date(d, 2026, retries=0) == []
    assert gs._read_scoreboard_cache(2026)[d]["ttl"] == gs._SCOREBOARD_TTL_EMPTY


def test_expired_entry_is_refetched(monkeypatch, tmp_path):
    """A cache entry past its TTL triggers a fresh fetch."""
    calls = _patch(monkeypatch, tmp_path, [_html(["1"]), _html(["1", "2"])])
    d = _mmddyyyy(0)
    gs.discover_contests_by_date(d, 2026)
    # Force the stored entry to look stale.
    cache = gs._read_scoreboard_cache(2026)
    cache[d]["ts"] = 0  # epoch 0 -> far past any TTL
    gs._write_scoreboard_cache(2026, cache)
    r = gs.discover_contests_by_date(d, 2026)
    assert r == ["1", "2"]
    assert calls["n"] == 2
