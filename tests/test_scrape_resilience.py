"""Unit tests for scrape_game_stats resilience (no network, no DB).

A flaky page must not abort the whole sweep: failed contests/teams are skipped (and
retried on the next run because they're never added to the resume ledger), while a
*systemic* failure rate still fails the run. discover_contests / fetch_contest_individual_stats
are monkeypatched so nothing touches stats.ncaa.org.
"""
from __future__ import annotations

import pandas as pd
import pytest

from vb.scrape import game_stats


def _fake_stats(contest_id: str) -> pd.DataFrame:
    """Minimal stat frame carrying the ContestID column the resume ledger reads."""
    return pd.DataFrame({"ContestID": [str(contest_id)], "Name": ["Player"], "Kills": [10]})


def _contest_ids(csv_path) -> set[str]:
    return set(pd.read_csv(csv_path, usecols=["ContestID"], dtype=str)["ContestID"])


def test_failed_contest_is_skipped_then_retried_next_run(tmp_path, monkeypatch):
    out = tmp_path / "stats.csv"
    monkeypatch.setattr(game_stats, "discover_contests", lambda tid: ["c1", "c2", "c3"])

    c2_broken = {"v": True}  # c2 fails on the first sweep, succeeds on the second

    def fake_fetch(cid: str) -> pd.DataFrame:
        if cid == "c2" and c2_broken["v"]:
            raise TimeoutError("boom")
        return _fake_stats(cid)

    monkeypatch.setattr(game_stats, "fetch_contest_individual_stats", fake_fetch)

    # First run: c2 fails and is skipped (not fatal — 0 teams failed), c1/c3 written.
    game_stats.scrape_game_stats(["100"], year=2026, output=out)
    assert _contest_ids(out) == {"c1", "c3"}

    # c2 was NOT recorded, so the next run retries it (c1/c3 skipped as already-seen).
    c2_broken["v"] = False
    game_stats.scrape_game_stats(["100"], year=2026, output=out)
    assert _contest_ids(out) == {"c1", "c2", "c3"}


def test_failed_team_below_threshold_returns(tmp_path, monkeypatch):
    out = tmp_path / "stats.csv"
    teams = ["1", "2", "3", "4", "5"]

    def fake_discover(tid: str) -> list[str]:
        if tid == "2":
            raise TimeoutError("team page hung")
        return [f"c{tid}"]

    monkeypatch.setattr(game_stats, "discover_contests", fake_discover)
    monkeypatch.setattr(game_stats, "fetch_contest_individual_stats", _fake_stats)

    # 1/5 = 20% <= 25% threshold -> completes, other four teams' contests written.
    game_stats.scrape_game_stats(teams, year=2026, output=out)
    assert _contest_ids(out) == {"c1", "c3", "c4", "c5"}


def test_systemic_failure_raises(tmp_path, monkeypatch):
    out = tmp_path / "stats.csv"
    teams = ["1", "2", "3", "4"]

    def fake_discover(tid: str) -> list[str]:
        if tid in {"2", "4"}:
            raise TimeoutError("team page hung")
        return [f"c{tid}"]

    monkeypatch.setattr(game_stats, "discover_contests", fake_discover)
    monkeypatch.setattr(game_stats, "fetch_contest_individual_stats", _fake_stats)

    # 2/4 = 50% > 25% threshold -> systemic failure.
    with pytest.raises(RuntimeError, match="scrape aborted"):
        game_stats.scrape_game_stats(teams, year=2026, output=out)
