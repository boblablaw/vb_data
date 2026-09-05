"""End-to-end PBP tests against Postgres (skipped if the DB is unreachable).

Seeds two synthetic teams + rosters + a contest, writes a tiny hand-built PBP CSV, then exercises
the full chain: ``load_pbp`` (events + team/player attribution + venue/attendance), ``derive_pbp``
(set attempts, assist %, setter hitting %, points played), and the ``/contests/{id}/pbp`` +
``/players/{id}/season-stats`` API surfaces.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from vb.db import engine, session_scope
from vb.derive import derive_cumulative, derive_pbp
from vb.load import load_pbp
from vb.models import (
    Conference,
    Contest,
    PbpEvent,
    Player,
    PlayerGameStat,
    PlayerPbpStat,
    Team,
    TeamSeasonId,
)
from vb.scrape.pbp import COLUMNS


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

SEASON = 1901  # sentinel season, distinct from test_derive's 1900
CID = "TESTPBP1"
AWAY_NCAA = "9000001"
HOME_NCAA = "9000002"

# One set, two rallies. Rally 1: away scores on Hitter A's kill (off Setter A's set).
# Rally 2: home's Hitter H attack-errors (off Setter H's set), so away scores again.
_ROWS = [
    # set, rally, seq, touch, player, side, terminal, ttype, scoring, away, home
    (1, 1, 1, "serve", "Home Server", "home", False, None, None, None, None),
    (1, 1, 2, "reception", "Libero A", "away", False, None, None, None, None),
    (1, 1, 3, "set", "Setter A", "away", False, None, None, None, None),
    (1, 1, 4, "attack", "Hitter A", "away", False, None, None, None, None),
    (1, 1, 5, "terminal", "Hitter A", "away", True, "kill", "away", 1, 0),
    (1, 2, 6, "serve", "Setter A", "away", False, None, None, None, None),
    (1, 2, 7, "reception", "Recv H", "home", False, None, None, None, None),
    (1, 2, 8, "set", "Setter H", "home", False, None, None, None, None),
    (1, 2, 9, "attack", "Hitter H", "home", False, None, None, None, None),
    # attack_error: charged to the erring (home) side; away scores.
    (1, 2, 10, "terminal", "Hitter H", "home", True, "attack_error", "away", 2, 0),
]

AWAY_PLAYERS = ["Setter A", "Hitter A", "Libero A"]
HOME_PLAYERS = ["Home Server", "Recv H", "Setter H", "Hitter H"]


def _wipe(s) -> None:
    s.execute(text("DELETE FROM pbp_events WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM player_pbp_stats WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM player_game_stats WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM contests WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM players WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM team_season_ids WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM teams WHERE name IN ('_TEST_AWAY', '_TEST_HOME')"))
    s.execute(text("DELETE FROM conferences WHERE name = '_TEST_PBP_CONF'"))


@pytest.fixture
def seeded(tmp_path) -> Path:
    with session_scope() as s:
        _wipe(s)
    ids = {}
    with session_scope() as s:
        conf = Conference(name="_TEST_PBP_CONF")
        s.add(conf)
        s.flush()
        away = Team(name="_TEST_AWAY", conference_id=conf.id)
        home = Team(name="_TEST_HOME", conference_id=conf.id)
        s.add_all([away, home])
        s.flush()
        s.add_all([
            TeamSeasonId(team_id=away.id, season=SEASON, ncaa_team_id=AWAY_NCAA),
            TeamSeasonId(team_id=home.id, season=SEASON, ncaa_team_id=HOME_NCAA),
        ])
        name_to_id = {}
        for nm in AWAY_PLAYERS:
            p = Player(team_id=away.id, season=SEASON, name=nm)
            s.add(p); s.flush(); name_to_id[nm] = p.id
        for nm in HOME_PLAYERS:
            p = Player(team_id=home.id, season=SEASON, name=nm)
            s.add(p); s.flush(); name_to_id[nm] = p.id
        s.add(Contest(contest_id=CID, season=SEASON,
                      home_team_id=home.id, away_team_id=away.id))
        s.flush()
        # A box-score line so the matview carries assists for Setter A -> assist_pct.
        s.add(PlayerGameStat(
            contest_id=CID, player_id=name_to_id["Setter A"], team_id=away.id,
            season=SEASON, sets=1, assists=3,
        ))
        s.flush()
        ids["setter_a"] = name_to_id["Setter A"]
        ids["setter_h"] = name_to_id["Setter H"]

    # Build the PBP CSV.
    df = pd.DataFrame([
        {
            "ContestID": CID, "Season": SEASON, "Set": r[0], "Rally": r[1], "Seq": r[2],
            "TouchType": r[3], "PlayerName": r[4], "Side": r[5], "IsTerminal": r[6],
            "TerminalType": r[7], "ScoringSide": r[8], "AwayScore": r[9], "HomeScore": r[10],
            "AwayNcaaId": AWAY_NCAA, "HomeNcaaId": HOME_NCAA,
            "Location": "Test Arena (Test City, TS)", "Attendance": 1234,
        }
        for r in _ROWS
    ], columns=COLUMNS)
    csv = tmp_path / "pbp.csv"
    df.to_csv(csv, index=False)

    yield csv, ids
    with session_scope() as s:
        _wipe(s)


@requires_db
def test_load_pbp_events_and_venue(seeded):
    csv, _ = seeded
    with session_scope() as s:
        res = load_pbp(s, SEASON, csv_path=csv)
    assert res["contests"] == 1
    assert res["events"] == 10
    assert res["unresolved_names"] == 0

    with session_scope() as s:
        events = (s.query(PbpEvent).filter_by(contest_id=CID)
                  .order_by(PbpEvent.seq).all())
        assert len(events) == 10
        # Every touch resolved to a team + player.
        assert all(e.team_id is not None for e in events)
        assert all(e.player_id is not None for e in events)
        # The kill terminal is charged to away; the attack_error to home.
        kill = next(e for e in events if e.terminal_type == "kill")
        err = next(e for e in events if e.terminal_type == "attack_error")
        assert kill.scoring_team_id == kill.team_id  # kill: owner == scorer (away)
        assert err.scoring_team_id != err.team_id     # error: charged to erring (home) side

        contest = s.get(Contest, CID)
        assert contest.location == "Test Arena (Test City, TS)"
        assert contest.attendance == 1234


@requires_db
def test_load_pbp_idempotent(seeded):
    csv, _ = seeded
    with session_scope() as s:
        load_pbp(s, SEASON, csv_path=csv)
    with session_scope() as s:
        load_pbp(s, SEASON, csv_path=csv)  # re-load: delete-then-insert
    with session_scope() as s:
        n = s.query(PbpEvent).filter_by(contest_id=CID).count()
    assert n == 10


@requires_db
def test_derive_pbp_setter_stats(seeded):
    csv, ids = seeded
    with session_scope() as s:
        load_pbp(s, SEASON, csv_path=csv)
    with session_scope() as s:
        derive_cumulative(s)  # refresh matview so assists are available
    with session_scope() as s:
        derive_pbp(s, SEASON)

    with session_scope() as s:
        a = s.get(PlayerPbpStat, (ids["setter_a"], SEASON))
        h = s.get(PlayerPbpStat, (ids["setter_h"], SEASON))
        assert a is not None and h is not None
        assert a.set_attempts == 1
        assert a.setter_hit_attacks == 1
        assert abs(a.setter_hitting_pct - 1.0) < 1e-6   # (1 kill - 0 err) / 1
        assert abs(a.assist_pct - 3.0) < 1e-6           # 3 season assists / 1 set attempt
        assert a.points_played == 2                     # starter, credited at both serves
        assert h.set_attempts == 1
        assert abs(h.setter_hitting_pct + 1.0) < 1e-6   # (0 - 1 err) / 1 = -1.0
        assert h.assist_pct is None                     # no box-score assists for Setter H


@requires_db
def test_pbp_api(seeded, client):
    csv, ids = seeded
    with session_scope() as s:
        load_pbp(s, SEASON, csv_path=csv)
    with session_scope() as s:
        derive_cumulative(s)
    with session_scope() as s:
        derive_pbp(s, SEASON)

    r = client.get(f"/contests/{CID}/pbp")
    assert r.status_code == 200
    body = r.json()
    assert len(body["sets"]) == 1
    st = body["sets"][0]
    assert st["set_number"] == 1
    assert st["away"]["kills"] == 1
    assert st["away"]["set_attempts"] == 1
    assert st["away"]["attack_attempts"] == 1
    assert st["home"]["errors"] == 1        # attack_error charged to home
    assert st["home"]["set_attempts"] == 1
    assert len(st["timeline"]) == 2

    # Advanced stats surface on the player season-stats endpoint.
    r2 = client.get(f"/players/{ids['setter_a']}/season-stats", params={"season": SEASON})
    assert r2.status_code == 200
    ss = r2.json()
    assert ss["set_attempts"] == 1
    assert abs(ss["assist_pct"] - 3.0) < 1e-6
    assert abs(ss["setter_hitting_pct"] - 1.0) < 1e-6
    assert ss["points_played"] == 2
