"""DB test: the game-stats loader persists linescore results onto contests.

Skipped when Postgres is unreachable. Builds a tiny sentinel-season fixture (two teams with
NCAA season ids + one player each) and a raw CSV carrying the ``AwaySetsWon`` / ``HomeSetsWon`` /
``SetScores`` columns the scraper now emits, then asserts the loaded ``Contest`` row.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest
from sqlalchemy import text

from vb.db import engine, session_scope
from vb.load import load_game_stats
from vb.models import Conference, Contest, Player, Team, TeamSeasonId


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

SEASON = 2102  # far-future sentinel
NCAA_HOME, NCAA_AWAY = "900001", "900002"


def _wipe(s):
    s.execute(text("DELETE FROM player_game_stats WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM contests WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM team_season_ids WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM players WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM teams WHERE name LIKE '_GS_TEAM%'"))
    s.execute(text("DELETE FROM conferences WHERE name LIKE '_GS_CONF%'"))


@pytest.fixture
def ids():
    with session_scope() as s:
        _wipe(s)
    with session_scope() as s:
        c = Conference(name="_GS_CONF"); s.add(c); s.flush()
        home = Team(name="_GS_TEAM_HOME", conference_id=c.id)
        away = Team(name="_GS_TEAM_AWAY", conference_id=c.id)
        s.add_all([home, away]); s.flush()
        s.add_all([
            TeamSeasonId(team_id=home.id, season=SEASON, ncaa_team_id=NCAA_HOME),
            TeamSeasonId(team_id=away.id, season=SEASON, ncaa_team_id=NCAA_AWAY),
        ])
        s.add(Player(team_id=home.id, season=SEASON, name="_GS P", ncaa_player_id="GSP1"))
        s.flush()
        out = {"home": home.id, "away": away.id}
    yield out
    with session_scope() as s:
        _wipe(s)


@requires_db
def test_load_game_stats_persists_linescore(tmp_path, ids):
    set_scores = {"away": [23, 25, 26, 22, 13], "home": [25, 13, 24, 25, 15]}
    csv = tmp_path / f"ncaa_wvb_game_stats_d1_{SEASON}.csv"
    # Build via pandas.to_csv so the JSON cell is quoted exactly as the scraper writes it.
    pd.DataFrame([{
        "TeamID": NCAA_HOME, "Season": SEASON, "ContestID": "7000001", "TeamSide": "Home",
        "PlayerID": "GSP1", "Date": "2026-09-01 18:00", "AwayTeamNcaaId": NCAA_AWAY,
        "HomeTeamNcaaId": NCAA_HOME, "AwaySetsWon": 2, "HomeSetsWon": 3,
        "SetScores": json.dumps(set_scores), "Name": "_GS P", "S": 3, "Kills": 10,
    }]).to_csv(csv, index=False)
    with session_scope() as s:
        load_game_stats(s, SEASON, csv)
    with session_scope() as s:
        c = s.get(Contest, "7000001")
        assert c.home_team_id == ids["home"] and c.away_team_id == ids["away"]
        assert c.home_sets_won == 3 and c.away_sets_won == 2   # home (UTEP-side) won
        assert c.set_scores == set_scores                       # JSONB round-trip
