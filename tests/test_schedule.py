"""Schedule scraper parsing (no DB) + loader/API integration (Postgres-backed, self-seeding).

The DB tests build a far-future sentinel season (teams A/B with NCAA season ids, a non-D1 team C
reachable only by name) so they never touch real data, and clean up after themselves. They're
skipped when Postgres is unreachable.
"""
from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import text

from vb.db import engine, session_scope
from vb.load import load_schedule
from vb.models import (
    Conference,
    Contest,
    Player,
    PlayerGameStat,
    Schedule,
    Team,
    TeamSeasonId,
)
from vb.scrape.schedule import _clean_opponent, _parse_opponent, _parse_schedule_rows


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

SEASON = 2103  # far-future sentinel
NCAA_A, NCAA_B = "910001", "910002"


# --------------------------------------------------------------------------- parsing (no DB)
def test_parse_opponent_site_variants():
    assert _parse_opponent("@ Houston") == ("away", "Houston", None)
    assert _parse_opponent("#5 Texas @ Austin, TX") == ("neutral", "Texas", "Austin, TX")
    assert _parse_opponent("Baylor (5-0)") == ("home", "Baylor", None)


def test_clean_opponent_strips_rank_and_record():
    assert _clean_opponent("#12 Stanford (10-2)") == "Stanford"
    assert _clean_opponent("RV Oregon") == "Oregon"
    assert _clean_opponent("Nebraska") == "Nebraska"


def test_parse_schedule_rows_extracts_dates_and_links():
    html = """
    <table><tbody>
      <tr><td>09/03/2026 07:30 PM</td>
          <td>@ <a href="/teams/12345">Houston</a></td><td></td></tr>
      <tr><td>09/06/2026</td>
          <td><a href="/teams/67890">Baylor</a> (5-0)</td><td>W 3-1</td></tr>
      <tr><td>Totals</td><td>ignored non-date row</td><td></td></tr>
    </tbody></table>
    """
    rows = _parse_schedule_rows(html)
    assert len(rows) == 2
    away = rows[0]
    assert away["Date"] == "2026-09-03" and away["Time"] == "07:30 PM"
    assert away["Site"] == "away" and away["OpponentName"] == "Houston"
    assert away["OpponentNcaaId"] == "12345"
    home = rows[1]
    assert home["Date"] == "2026-09-06" and home["Site"] == "home"
    assert home["OpponentName"] == "Baylor" and home["OpponentNcaaId"] == "67890"
    assert home["ResultRaw"] == "W 3-1"


# --------------------------------------------------------------------------- DB fixtures
def _wipe(s):
    s.execute(text("DELETE FROM schedule WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM player_game_stats WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM contests WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM team_season_ids WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM players WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM teams WHERE name LIKE '_SCH_TEAM%'"))
    s.execute(text("DELETE FROM conferences WHERE name LIKE '_SCH_CONF%'"))


@pytest.fixture
def seed():
    """A/B carry NCAA season ids (resolvable by id); C exists only in `teams` (name fallback)."""
    with session_scope() as s:
        _wipe(s)
    with session_scope() as s:
        conf = Conference(name="_SCH_CONF"); s.add(conf); s.flush()
        a = Team(name="_SCH_TEAM_A", short_name="SchA", conference_id=conf.id)
        b = Team(name="_SCH_TEAM_B", short_name="SchB", conference_id=conf.id)
        c = Team(name="_SCH_TEAM_C", short_name="SchC", conference_id=conf.id)
        s.add_all([a, b, c]); s.flush()
        s.add_all([
            TeamSeasonId(team_id=a.id, season=SEASON, ncaa_team_id=NCAA_A),
            TeamSeasonId(team_id=b.id, season=SEASON, ncaa_team_id=NCAA_B),
        ])
        out = {"a": a.id, "b": b.id, "c": c.id}
    yield out
    with session_scope() as s:
        _wipe(s)


def _schedule_csv(tmp_path):
    """Three rows: opponent by NCAA id, opponent by name, and an unresolvable opponent."""
    csv = tmp_path / f"ncaa_wvb_schedule_d1_{SEASON}.csv"
    pd.DataFrame([
        {"Season": f"{SEASON}-{SEASON + 1}", "TeamNcaaId": NCAA_A, "Date": "2103-09-08",
         "Time": "07:30 PM", "OpponentName": "_SCH_TEAM_B", "OpponentNcaaId": NCAA_B,
         "Site": "home", "NeutralLocation": "", "ResultRaw": ""},
        {"Season": f"{SEASON}-{SEASON + 1}", "TeamNcaaId": NCAA_A, "Date": "2103-09-12",
         "Time": "", "OpponentName": "_SCH_TEAM_C", "OpponentNcaaId": "",
         "Site": "away", "NeutralLocation": "", "ResultRaw": ""},
        {"Season": f"{SEASON}-{SEASON + 1}", "TeamNcaaId": NCAA_A, "Date": "2103-09-15",
         "Time": "", "OpponentName": "Nowhere Junior College", "OpponentNcaaId": "",
         "Site": "home", "NeutralLocation": "", "ResultRaw": ""},
    ]).to_csv(csv, index=False)
    return csv


# --------------------------------------------------------------------------- loader
@requires_db
def test_load_schedule_resolves_and_upserts(tmp_path, seed):
    csv = _schedule_csv(tmp_path)
    with session_scope() as s:
        res = load_schedule(s, SEASON, csv)
    assert res == {"inserted": 3, "updated": 0, "skipped": 0, "unresolved_opponents": 1}

    with session_scope() as s:
        rows = {
            r.date: r for r in s.query(Schedule).filter(Schedule.season == SEASON).all()
        }
        assert rows["2103-09-08"].opponent_team_id == seed["b"]   # resolved by NCAA id
        assert rows["2103-09-12"].opponent_team_id == seed["c"]   # resolved by name fallback
        assert rows["2103-09-15"].opponent_team_id is None        # unresolved -> NULL
        assert rows["2103-09-08"].site == "home"


@requires_db
def test_load_schedule_idempotent_reload(tmp_path, seed):
    csv = _schedule_csv(tmp_path)
    with session_scope() as s:
        load_schedule(s, SEASON, csv)
    with session_scope() as s:
        res = load_schedule(s, SEASON, csv)
    assert res["inserted"] == 0 and res["updated"] == 3
    with session_scope() as s:
        assert s.query(Schedule).filter(Schedule.season == SEASON).count() == 3


# --------------------------------------------------------------------------- API integration
@pytest.fixture
def seed_games(seed):
    """Add a played contest (A vs B, with box score) + an upcoming game (both schedule sides)."""
    with session_scope() as s:
        a, b = seed["a"], seed["b"]
        s.add(Contest(
            contest_id="7100001", season=SEASON, date="2103-09-01",
            home_team_id=a, away_team_id=b, home_sets_won=3, away_sets_won=1,
            set_scores={"home": [25, 22, 25, 25], "away": [20, 25, 18, 21]},
        ))
        pa = Player(team_id=a, season=SEASON, name="_SCH Ann", ncaa_player_id="SCHPA")
        pb = Player(team_id=b, season=SEASON, name="_SCH Bea", ncaa_player_id="SCHPB")
        s.add_all([pa, pb]); s.flush()
        s.add_all([
            PlayerGameStat(contest_id="7100001", player_id=pa.id, team_id=a, season=SEASON,
                           sets=4, kills=15, pts=18),
            PlayerGameStat(contest_id="7100001", player_id=pb.id, team_id=b, season=SEASON,
                           sets=4, kills=9, pts=11),
        ])
        # Upcoming A-vs-B game, present as both per-team perspectives (to test scoreboard dedupe).
        s.add_all([
            Schedule(season=SEASON, team_id=a, opponent_team_id=b, opponent_name="_SCH_TEAM_B",
                     date="2103-09-08", game_time="07:30 PM", site="home"),
            Schedule(season=SEASON, team_id=b, opponent_team_id=a, opponent_name="_SCH_TEAM_A",
                     date="2103-09-08", game_time="07:30 PM", site="away"),
        ])
    return seed


@requires_db
def test_team_games_merges_played_and_upcoming(client, seed_games):
    a = seed_games["a"]
    games = client.get(f"/teams/{a}/games", params={"season": SEASON}).json()
    by_date = {g["date"]: g for g in games}
    played = by_date["2103-09-01"]
    assert played["status"] == "played" and played["contest_id"] == "7100001"
    assert played["result"] == "W" and played["team_sets_won"] == 3
    assert played["opponent_id"] == seed_games["b"]
    upcoming = by_date["2103-09-08"]
    assert upcoming["status"] == "upcoming" and upcoming["contest_id"] is None
    assert upcoming["game_time"] == "07:30 PM" and upcoming["site"] == "home"


@requires_db
def test_scoreboard_dedupes_and_reports_scores(client, seed_games):
    played = client.get("/games", params={"season": SEASON, "date": "2103-09-01"}).json()
    assert len(played) == 1
    assert played[0]["status"] == "played"
    assert played[0]["home_sets_won"] == 3 and played[0]["away_sets_won"] == 1

    upcoming = client.get("/games", params={"season": SEASON, "date": "2103-09-08"}).json()
    assert len(upcoming) == 1  # two schedule perspectives collapsed into one game
    assert upcoming[0]["status"] == "upcoming"


@requires_db
def test_contest_detail_and_box_score(client, seed_games):
    c = client.get("/contests/7100001").json()
    assert c["home_team"]["id"] == seed_games["a"] and c["away_team"]["id"] == seed_games["b"]
    assert c["set_scores"]["home"] == [25, 22, 25, 25]

    stats = client.get("/contests/7100001/stats").json()
    names = {row["player_name"] for row in stats}
    assert "_SCH Ann" in names and "_SCH Bea" in names
