"""Live-score overlay merged into /games from the henrygd ncaa.com sidecar.

``_merge_live_board`` takes the played+upcoming union the endpoint has built and overlays in-progress
/ just-finished ncaa.com scores onto the *upcoming* stubs (an authoritative played ``Contest`` is
never touched). These tests exercise the merge in isolation — no DB, no network — by stubbing
``ncaa_api.scoreboard_cached`` and freezing "today" so the today/yesterday window matches the seed.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from vb.api import live_merge
from vb.api.routers import games as games_mod
from vb.api.schemas import ScoreboardGame, TeamRef
from vb.scrape.ncaa_api import ApiGame, ApiLinescore

_ET = ZoneInfo("America/New_York")
TODAY = date(2104, 9, 8)
START, END_EXCL = TODAY.isoformat(), (date(2104, 9, 9)).isoformat()


@pytest.fixture(autouse=True)
def _no_linescores(monkeypatch):
    """Default: no per-set overlay (and no network). Set-score tests override this per-case."""
    monkeypatch.setattr(live_merge.ncaa_api, "game_linescores", lambda gid: None)


def _stub_linescores(monkeypatch, ls):
    monkeypatch.setattr(live_merge.ncaa_api, "game_linescores", lambda gid: ls)


def _freeze_today(monkeypatch):
    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2104, 9, 8, 20, 0, tzinfo=tz or _ET)
    monkeypatch.setattr(games_mod, "_datetime", _FakeDatetime)


def _stub_board(monkeypatch, board):
    monkeypatch.setattr(live_merge.ncaa_api, "scoreboard_cached", lambda day: board)


def _team(tid, short):
    return TeamRef(id=tid, name=short, short_name=short)


def _upcoming(ncaa_id="6300002", home="Zqmapa Tech", away="Zqmapb St."):
    return ScoreboardGame(
        date=f"{TODAY.isoformat()} 18:00", status="upcoming", contest_id="c1",
        ncaa_game_id=ncaa_id, home_team=_team(1, home), away_team=_team(2, away),
    )


def _api_game(state, home_sets, away_sets, ncaa_id="6300002",
              seonames=("zqmapb-st", "zqmapa-tech"), period="4TH SET"):
    # ncaa.com lists seonames (away, home); home_sets_won maps to the home side (seonames[1]).
    return ApiGame(
        ncaa_game_id=ncaa_id, date=TODAY.isoformat(), seonames=seonames,
        name_shorts=("Zqmapb St.", "Zqmapa Tech"), start_epoch=None, game_state=state,
        home_sets_won=home_sets, away_sets_won=away_sets, current_period=period,
    )


def test_live_overlay_by_id_aligned(monkeypatch):
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("live", home_sets=2, away_sets=1)])
    g = _upcoming()
    assert games_mod._merge_live_board([g], START, END_EXCL) is True
    assert g.status == "live"
    assert (g.home_sets_won, g.away_sets_won) == (2, 1)  # our home == ncaa home -> aligned
    assert g.live_period == "4TH SET"


def test_live_overlay_orientation_flipped_for_neutral(monkeypatch):
    """When our home side is ncaa.com's *away* team, the sets-won are swapped onto our slots."""
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("live", home_sets=2, away_sets=1)])
    # Our home team is "Zqmapb St." (ncaa's away). ncaa away.score = 1 -> our home should read 1.
    g = _upcoming(home="Zqmapb St.", away="Zqmapa Tech")
    assert games_mod._merge_live_board([g], START, END_EXCL) is True
    assert (g.home_sets_won, g.away_sets_won) == (1, 2)


def test_live_overlay_attaches_set_scores_aligned(monkeypatch):
    """Per-set points from the game endpoint land on our home/away slots (aligned orientation)."""
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("live", home_sets=2, away_sets=1)])
    # ncaa home won sets 1&2 (25-20, 25-23) and leads set 3 (9-11 -> away up); home=our home here.
    _stub_linescores(monkeypatch, ApiLinescore(home=(25, 25, 9), visit=(20, 23, 11)))
    g = _upcoming()
    assert games_mod._merge_live_board([g], START, END_EXCL) is True
    assert g.set_scores == {"home": [25, 25, 9], "away": [20, 23, 11]}


def test_live_overlay_set_scores_flipped(monkeypatch):
    """When our home is ncaa.com's away team, the per-set points swap onto our slots too."""
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("live", home_sets=2, away_sets=1)])
    _stub_linescores(monkeypatch, ApiLinescore(home=(25, 25, 9), visit=(20, 23, 11)))
    g = _upcoming(home="Zqmapb St.", away="Zqmapa Tech")  # our home == ncaa away
    assert games_mod._merge_live_board([g], START, END_EXCL) is True
    assert g.set_scores == {"home": [20, 23, 11], "away": [25, 25, 9]}


def test_live_overlay_missing_linescores_leaves_sets_won(monkeypatch):
    """A sidecar miss on the per-game endpoint leaves the sets-won lines, no set_scores."""
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("live", home_sets=2, away_sets=1)])
    _stub_linescores(monkeypatch, None)
    g = _upcoming()
    assert games_mod._merge_live_board([g], START, END_EXCL) is True
    assert g.set_scores is None
    assert (g.home_sets_won, g.away_sets_won) == (2, 1)


def test_final_becomes_final_pending(monkeypatch):
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("final", home_sets=3, away_sets=0)])
    g = _upcoming()
    assert games_mod._merge_live_board([g], START, END_EXCL) is True
    assert g.status == "final_pending"
    assert (g.home_sets_won, g.away_sets_won) == (3, 0)


def test_played_contest_is_never_overwritten(monkeypatch):
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("final", home_sets=3, away_sets=0)])
    g = _upcoming()
    g.status = "played"
    g.home_sets_won, g.away_sets_won = 1, 3  # authoritative scrape says the away team won
    assert games_mod._merge_live_board([g], START, END_EXCL) is False
    assert g.status == "played"
    assert (g.home_sets_won, g.away_sets_won) == (1, 3)  # untouched


def test_fallback_match_by_team_pair_when_no_id(monkeypatch):
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("live", home_sets=1, away_sets=2)])
    g = _upcoming(ncaa_id=None)  # not yet mapped -> must match on date + team pair
    assert games_mod._merge_live_board([g], START, END_EXCL) is True
    assert g.status == "live"
    assert (g.home_sets_won, g.away_sets_won) == (1, 2)


def test_pre_game_left_as_upcoming(monkeypatch):
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("pre", home_sets=None, away_sets=None)])
    g = _upcoming()
    assert games_mod._merge_live_board([g], START, END_EXCL) is False
    assert g.status == "upcoming"


def test_no_merge_off_today_window(monkeypatch):
    """A window that excludes today/yesterday never calls the sidecar and returns False."""
    _freeze_today(monkeypatch)

    def _boom(day):
        raise AssertionError("sidecar must not be called for an off-window range")
    monkeypatch.setattr(live_merge.ncaa_api, "scoreboard_cached", _boom)
    g = _upcoming()
    g.date = "2104-08-01 18:00"
    assert games_mod._merge_live_board([g], "2104-08-01", "2104-08-02") is False
    assert g.status == "upcoming"


def test_sidecar_failure_degrades_gracefully(monkeypatch):
    _freeze_today(monkeypatch)

    def _fail(day):
        raise RuntimeError("sidecar down")
    monkeypatch.setattr(live_merge.ncaa_api, "scoreboard_cached", _fail)
    g = _upcoming()
    assert games_mod._merge_live_board([g], START, END_EXCL) is False
    assert g.status == "upcoming"  # board served unchanged
