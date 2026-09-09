"""Parsing tests for the henrygd/ncaa-api client (no network — ``_get`` is monkeypatched).

Fixtures mirror the real WVB shapes returned by the sidecar (captured from a live container):
scoreboard games wrap each game under ``game`` with ``home``/``away`` name blocks; the boxscore
groups ``playerStats`` under ``teamBoxscore`` by numeric teamId, with a separate ``teams`` block that
maps teamId -> seoname.
"""
from __future__ import annotations

from datetime import date

import vb.scrape.ncaa_api as api

_SCOREBOARD = {
    "games": [
        {"game": {
            "gameID": "6482659",
            "startDate": "09/05/2025",
            "startTimeEpoch": "1757048400",
            "gameState": "final",
            "away": {"score": "3", "names": {"short": "San Jose St.", "seo": "san-jose-st"}},
            "home": {"score": "2", "names": {"short": "Hawaii", "seo": "hawaii"}},
        }},
        {"game": {  # a game with no id is skipped
            "gameID": "",
            "startDate": "09/05/2025",
            "away": {"names": {"short": "A", "seo": "a"}},
            "home": {"names": {"short": "B", "seo": "b"}},
        }},
    ]
}

_BOXSCORE = {
    "teams": [
        {"teamId": "46419", "seoname": "hawaii"},
        {"teamId": "46494", "seoname": "san-jose-st"},
    ],
    "teamBoxscore": [
        {"teamId": 46419, "playerStats": [
            {"firstName": "Bri", "lastName": "Gunderson", "position": "MB", "number": 8,
             "gamesPlayed": "5", "kills": "15", "attackErrors": "1", "attackAttempts": "22",
             "assists": "0", "serviceAces": "3", "serviceErrors": "3", "digs": "3",
             "receptionAttempts": "0", "receptionErrors": "0", "blockSolos": "1",
             "blockAssists": "4", "blockingErrors": "0", "ballHandlingErrors": "0",
             "points": "21", "starter": True, "participated": True},
        ]},
        {"teamId": 46494, "playerStats": [
            {"firstName": "Reserve", "lastName": "Player", "number": None, "kills": "",
             "starter": False, "participated": False},
        ]},
    ],
}


def test_scoreboard_parses_and_skips_idless(monkeypatch):
    monkeypatch.setattr(api, "_get", lambda path, session=None: _SCOREBOARD)
    games = api.scoreboard(date(2025, 9, 5))
    assert len(games) == 1                       # the id-less game is dropped
    g = games[0]
    assert g.ncaa_game_id == "6482659"
    assert g.date == "2025-09-05"                # MM/DD/YYYY -> ISO
    assert g.seonames == ("san-jose-st", "hawaii")   # (away, home)
    assert g.name_shorts == ("San Jose St.", "Hawaii")
    assert g.start_epoch == 1757048400
    assert g.game_state == "final"


def test_boxscore_maps_teamid_to_seoname_and_stats(monkeypatch):
    monkeypatch.setattr(api, "_get", lambda path, session=None: _BOXSCORE)
    box = api.boxscore("6482659")
    assert box.ncaa_game_id == "6482659"
    assert len(box.lines) == 2
    bri = box.lines[0]
    assert bri.seoname == "hawaii"               # resolved from the teams block
    assert bri.ncaa_team_id == "46419"
    assert bri.name == "Bri Gunderson"
    assert bri.number == 8
    assert bri.starter is True and bri.participated is True
    assert bri.kills == 15.0
    assert bri.attack_errors == 1.0
    assert bri.service_aces == 3.0
    assert bri.block_assists == 4.0
    assert bri.points == 21.0

    reserve = box.lines[1]
    assert reserve.seoname == "san-jose-st"
    assert reserve.number is None
    assert reserve.kills is None                 # blank string -> None
    assert reserve.starter is False and reserve.participated is False


def test_iso_date_handles_bad_input():
    assert api._iso_date("09/05/2025") == "2025-09-05"
    assert api._iso_date("") == ""
    assert api._iso_date("not-a-date") == ""


def test_numeric_parsers():
    assert api._f("3") == 3.0
    assert api._f("0.636") == 0.636
    assert api._f("") is None
    assert api._f(None) is None
    assert api._i("8") == 8
    assert api._i("") is None
