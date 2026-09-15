"""Live-score overlay merged into /teams/{id}/games from the henrygd ncaa.com sidecar.

``_merge_live_team_rows`` overlays in-progress / just-finished ncaa.com scores onto a team's
*upcoming* schedule rows, oriented onto the team-relative ``team_sets_won`` / ``opponent_sets_won``
slots (self is the home side only when ``site == 'home'``). Same sidecar source as the league
scoreboard; these tests exercise the merge in isolation — no DB, no network — by stubbing
``live_merge.ncaa_api`` and freezing "today" so the today/yesterday window matches the seed.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from vb.api import live_merge
from vb.api.routers import teams as teams_mod
from vb.api.schemas import TeamGameRow
from vb.scrape.ncaa_api import ApiGame, ApiLinescore

_ET = ZoneInfo("America/New_York")
TODAY = date(2104, 9, 8)

# self is "Zqmapa Tech" (slug zqmapa-tech); opponent "Zqmapb St." (slug zqmapb-st).
SELF_SLUG = "zqmapa-tech"


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
    monkeypatch.setattr(teams_mod, "_datetime", _FakeDatetime)


def _stub_board(monkeypatch, board):
    monkeypatch.setattr(live_merge.ncaa_api, "scoreboard_cached", lambda day: board)


def _row(site="home", ncaa_id="6300002", opponent="Zqmapb St."):
    return TeamGameRow(
        date=f"{TODAY.isoformat()} 18:00", status="upcoming", site=site,
        ncaa_game_id=ncaa_id, opponent=opponent, opponent_short=opponent,
    )


def _api_game(state, home_sets, away_sets, ncaa_id="6300002",
              seonames=("zqmapb-st", "zqmapa-tech"), period="4TH SET"):
    # ncaa.com lists seonames (away, home); home_sets_won maps to the home side (seonames[1]).
    return ApiGame(
        ncaa_game_id=ncaa_id, date=TODAY.isoformat(), seonames=seonames,
        name_shorts=("Zqmapb St.", "Zqmapa Tech"), start_epoch=None, game_state=state,
        home_sets_won=home_sets, away_sets_won=away_sets, current_period=period,
    )


def test_live_overlay_home_game(monkeypatch):
    """Self is the ncaa home side: sets-won land directly on team/opponent slots."""
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("live", home_sets=2, away_sets=1)])
    r = _row(site="home")
    assert teams_mod._merge_live_team_rows([r], SELF_SLUG) is True
    assert r.status == "live"
    assert (r.team_sets_won, r.opponent_sets_won) == (2, 1)
    assert r.live_period == "4TH SET"


def test_live_overlay_away_game(monkeypatch):
    """Self plays away (opponent is the nominal ncaa home side): sets-won swap onto our slots."""
    _freeze_today(monkeypatch)
    # Board: self "Zqmapa Tech" is the ncaa *away* team here; ncaa away won 1 set.
    board = [_api_game("live", home_sets=2, away_sets=1,
                       seonames=("zqmapa-tech", "zqmapb-st"))]
    _stub_board(monkeypatch, board)
    r = _row(site="away")
    assert teams_mod._merge_live_team_rows([r], SELF_SLUG) is True
    # self is ncaa away (1 set), opponent is ncaa home (2 sets)
    assert (r.team_sets_won, r.opponent_sets_won) == (1, 2)


def test_set_scores_home_keyed(monkeypatch):
    """Per-set points are stored home/away-keyed; the client orients by site."""
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("live", home_sets=2, away_sets=1)])
    _stub_linescores(monkeypatch, ApiLinescore(home=(25, 25, 9), visit=(20, 23, 11)))
    r = _row(site="home")
    assert teams_mod._merge_live_team_rows([r], SELF_SLUG) is True
    assert r.set_scores == {"home": [25, 25, 9], "away": [20, 23, 11]}


def test_final_becomes_final_pending(monkeypatch):
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("final", home_sets=3, away_sets=0)])
    r = _row(site="home")
    assert teams_mod._merge_live_team_rows([r], SELF_SLUG) is True
    assert r.status == "final_pending"
    assert (r.team_sets_won, r.opponent_sets_won) == (3, 0)


def test_played_row_is_never_touched(monkeypatch):
    """Only 'upcoming' rows are targets; an authoritative played result stays put."""
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("final", home_sets=3, away_sets=0)])
    r = _row(site="home")
    r.status = "played"
    r.team_sets_won, r.opponent_sets_won = 1, 3
    assert teams_mod._merge_live_team_rows([r], SELF_SLUG) is False
    assert r.status == "played"
    assert (r.team_sets_won, r.opponent_sets_won) == (1, 3)


def test_fallback_match_by_team_pair_when_no_id(monkeypatch):
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("live", home_sets=2, away_sets=1)])
    r = _row(site="home", ncaa_id=None)  # not yet mapped -> match on date + team pair
    assert teams_mod._merge_live_team_rows([r], SELF_SLUG) is True
    assert r.status == "live"
    assert (r.team_sets_won, r.opponent_sets_won) == (2, 1)


def test_pre_game_left_as_upcoming(monkeypatch):
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("pre", home_sets=None, away_sets=None)])
    r = _row(site="home")
    assert teams_mod._merge_live_team_rows([r], SELF_SLUG) is False
    assert r.status == "upcoming"


def test_no_merge_off_today_window(monkeypatch):
    """A row dated outside today/yesterday never calls the sidecar and returns False."""
    _freeze_today(monkeypatch)

    def _boom(day):
        raise AssertionError("sidecar must not be called for an off-window row")
    monkeypatch.setattr(live_merge.ncaa_api, "scoreboard_cached", _boom)
    r = _row(site="home")
    r.date = "2104-08-01 18:00"
    assert teams_mod._merge_live_team_rows([r], SELF_SLUG) is False
    assert r.status == "upcoming"


def test_sidecar_failure_degrades_gracefully(monkeypatch):
    _freeze_today(monkeypatch)

    def _fail(day):
        raise RuntimeError("sidecar down")
    monkeypatch.setattr(live_merge.ncaa_api, "scoreboard_cached", _fail)
    r = _row(site="home")
    assert teams_mod._merge_live_team_rows([r], SELF_SLUG) is False
    assert r.status == "upcoming"


def test_no_self_slug_is_noop(monkeypatch):
    """Without a self slug there is nothing to orient against — served unchanged."""
    _freeze_today(monkeypatch)
    _stub_board(monkeypatch, [_api_game("live", home_sets=2, away_sets=1)])
    r = _row(site="home")
    assert teams_mod._merge_live_team_rows([r], "") is False
    assert r.status == "upcoming"
