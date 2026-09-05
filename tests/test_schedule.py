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
          <td>@ <a href="/teams/12345">Houston</a></td>
          <td><a href="/contests/6628177">Preview</a></td></tr>
      <tr><td>09/06/2026</td>
          <td><a href="/teams/67890">Baylor</a> (5-0)</td>
          <td><a href="/contests/6591466/box_score">W 3-1</a></td></tr>
      <tr><td>Totals</td><td>ignored non-date row</td><td></td></tr>
    </tbody></table>
    """
    rows = _parse_schedule_rows(html)
    assert len(rows) == 2
    away = rows[0]
    assert away["Date"] == "2026-09-03" and away["Time"] == "07:30 PM"
    assert away["Site"] == "away" and away["OpponentName"] == "Houston"
    assert away["OpponentNcaaId"] == "12345"
    # Upcoming row: the /contests/<id> matchup link (no /box_score) is the NCAA game id.
    assert away["ContestId"] == "6628177"
    home = rows[1]
    assert home["Date"] == "2026-09-06" and home["Site"] == "home"
    assert home["OpponentName"] == "Baylor" and home["OpponentNcaaId"] == "67890"
    assert home["ResultRaw"] == "W 3-1"
    # Played row: the id also comes through from the /contests/<id>/box_score link.
    assert home["ContestId"] == "6591466"


def test_parse_schedule_rows_missing_contest_link_is_blank():
    html = """
    <table><tbody>
      <tr><td>09/03/2026 07:30 PM</td>
          <td>@ <a href="/teams/12345">Houston</a></td><td></td></tr>
    </tbody></table>
    """
    rows = _parse_schedule_rows(html)
    assert rows[0]["ContestId"] == ""


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
         "Site": "home", "NeutralLocation": "", "ResultRaw": "", "ContestId": "6628177"},
        {"Season": f"{SEASON}-{SEASON + 1}", "TeamNcaaId": NCAA_A, "Date": "2103-09-12",
         "Time": "", "OpponentName": "_SCH_TEAM_C", "OpponentNcaaId": "",
         "Site": "away", "NeutralLocation": "", "ResultRaw": "", "ContestId": ""},
        {"Season": f"{SEASON}-{SEASON + 1}", "TeamNcaaId": NCAA_A, "Date": "2103-09-15",
         "Time": "", "OpponentName": "Nowhere Junior College", "OpponentNcaaId": "",
         "Site": "home", "NeutralLocation": "", "ResultRaw": "", "ContestId": ""},
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
        assert rows["2103-09-08"].contest_id == "6628177"         # captured NCAA game id
        assert rows["2103-09-12"].contest_id is None              # blank cell -> NULL


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
        pa = Player(team_id=a, season=SEASON, name="_SCH Ann", ncaa_player_id="SCHPA", number=7)
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
def test_scoreboard_upcoming_carries_ncaa_contest_id(client, seed):
    """An upcoming game surfaces its NCAA contest_id so the UI can link out to ncaa.com/game/<id>
    before the box score is scraped."""
    a, b = seed["a"], seed["b"]
    with session_scope() as s:
        s.add_all([
            Schedule(season=SEASON, team_id=a, opponent_team_id=b, opponent_name="_SCH_TEAM_B",
                     date="2103-10-04", game_time="07:30 PM", site="home", contest_id="6628177"),
            Schedule(season=SEASON, team_id=b, opponent_team_id=a, opponent_name="_SCH_TEAM_A",
                     date="2103-10-04", game_time="07:30 PM", site="away", contest_id="6628177"),
        ])
    games = client.get("/games", params={"season": SEASON, "date": "2103-10-04"}).json()
    assert len(games) == 1  # two perspectives deduped
    assert games[0]["status"] == "upcoming"
    assert games[0]["contest_id"] == "6628177"


@requires_db
def test_scoreboard_dedupes_and_reports_scores(client, seed_games):
    played = client.get("/games", params={"season": SEASON, "date": "2103-09-01"}).json()
    assert len(played) == 1
    assert played[0]["status"] == "played"
    assert played[0]["home_sets_won"] == 3 and played[0]["away_sets_won"] == 1


@requires_db
def test_scoreboard_team_refs_carry_conference_id(client, seed_games):
    """TeamRefs embedded in the scoreboard expose conference_id (drives the Games conf filter)."""
    g = client.get("/games", params={"season": SEASON, "date": "2103-09-01"}).json()[0]
    home_conf = g["home_team"]["conference_id"]
    away_conf = g["away_team"]["conference_id"]
    assert home_conf is not None
    assert home_conf == away_conf   # A and B share "_SCH_CONF" in the fixture

    upcoming = client.get("/games", params={"season": SEASON, "date": "2103-09-08"}).json()
    assert len(upcoming) == 1  # two schedule perspectives collapsed into one game
    assert upcoming[0]["status"] == "upcoming"


@requires_db
def test_scoreboard_dedupes_played_over_schedule_stub_with_time_suffix(client, seed_games):
    """Regression: a played contest carries a time suffix on its date ("...-08 19:30") while the
    schedule stub is a bare day ("...-08") and its result_raw is still NULL (the weekly schedule
    scrape hasn't caught up). Dedup must compare on the day only, or the upcoming stub shows up
    right next to the played result."""
    a, b = seed_games["a"], seed_games["b"]
    with session_scope() as s:
        s.add(Contest(
            contest_id="7100002", season=SEASON, date="2103-09-08 19:30",
            home_team_id=a, away_team_id=b, home_sets_won=3, away_sets_won=0,
            set_scores={"home": [25, 25, 25], "away": [20, 18, 21]},
        ))

    games = client.get("/games", params={"season": SEASON, "date": "2103-09-08"}).json()
    assert len(games) == 1  # the still-"upcoming" schedule stub is deduped by the played contest
    assert games[0]["status"] == "played"
    assert games[0]["contest_id"] == "7100002"

    # Same collapse on the team-schedule view.
    team_games = client.get(f"/teams/{a}/games", params={"season": SEASON}).json()
    on_day = [g for g in team_games if (g["date"] or "")[:10] == "2103-09-08"]
    assert len(on_day) == 1 and on_day[0]["status"] == "played"


@requires_db
def test_contest_detail_and_box_score(client, seed_games):
    c = client.get("/contests/7100001").json()
    assert c["home_team"]["id"] == seed_games["a"] and c["away_team"]["id"] == seed_games["b"]
    assert c["set_scores"]["home"] == [25, 22, 25, 25]

    stats = client.get("/contests/7100001/stats").json()
    names = {row["player_name"] for row in stats}
    assert "_SCH Ann" in names and "_SCH Bea" in names
    # Box score carries the jersey number (shown in the player cell); NULL when unknown.
    by_name = {row["player_name"]: row for row in stats}
    assert by_name["_SCH Ann"]["number"] == 7
    assert by_name["_SCH Bea"]["number"] is None


@requires_db
def test_scoreboard_dedupes_timezone_rolled_contest(client, seed):
    """A late Hawaii/Pacific match gets its ``contests.date`` rolled into the next day's small
    hours ("...-14 01:00") while the schedule stub keeps the real local day ("...-13"). The stub
    must still be deduped even though the two disagree on the calendar day."""
    a, b = seed["a"], seed["b"]
    with session_scope() as s:
        s.add(Contest(
            contest_id="7100010", season=SEASON, date="2103-09-14 01:00",
            home_team_id=a, away_team_id=b, home_sets_won=3, away_sets_won=1,
            set_scores={"home": [25, 25, 20, 25], "away": [20, 18, 25, 21]},
        ))
        s.add_all([
            Schedule(season=SEASON, team_id=a, opponent_team_id=b, opponent_name="_SCH_TEAM_B",
                     date="2103-09-13", game_time="10:00 PM", site="home"),
            Schedule(season=SEASON, team_id=b, opponent_team_id=a, opponent_name="_SCH_TEAM_A",
                     date="2103-09-13", game_time="10:00 PM", site="away"),
        ])
    games = client.get("/games", params={
        "season": SEASON, "start": "2103-09-13", "end": "2103-09-14"}).json()
    assert len(games) == 1  # the "13th" stub collapses into the played contest (emitted on the 14th)
    assert games[0]["status"] == "played" and games[0]["contest_id"] == "7100010"


@requires_db
def test_scoreboard_keeps_back_to_back_rematch(client, seed):
    """Teams sometimes play on consecutive days. A played contest on day 1 must NOT dedup the
    scheduled rematch stub on day 2 — its evening start marks it a real second game, not a
    timezone-rolled duplicate."""
    a, b = seed["a"], seed["b"]
    with session_scope() as s:
        s.add(Contest(
            contest_id="7100011", season=SEASON, date="2103-09-20 18:00",
            home_team_id=a, away_team_id=b, home_sets_won=3, away_sets_won=0,
            set_scores={"home": [25, 25, 25], "away": [20, 18, 21]},
        ))
        s.add_all([
            Schedule(season=SEASON, team_id=a, opponent_team_id=b, opponent_name="_SCH_TEAM_B",
                     date="2103-09-21", game_time="06:00 PM", site="home"),
            Schedule(season=SEASON, team_id=b, opponent_team_id=a, opponent_name="_SCH_TEAM_A",
                     date="2103-09-21", game_time="06:00 PM", site="away"),
        ])
    games = client.get("/games", params={
        "season": SEASON, "start": "2103-09-20", "end": "2103-09-21"}).json()
    assert len(games) == 2  # both the played day-1 game and the upcoming day-2 rematch survive
    assert {g["status"] for g in games} == {"played", "upcoming"}


@requires_db
def test_scoreboard_dedupes_non_d1_opponent(client, seed):
    """A game vs a non-D1 opponent has no opponent Team row: the schedule stub carries only a name
    and the played contest has a NULL other side, so there's no id to pair on. Dedup them on
    (day, team) so the finished game doesn't show twice."""
    a = seed["a"]
    with session_scope() as s:
        s.add(Contest(
            contest_id="7100012", season=SEASON, date="2103-09-25 13:00",
            home_team_id=a, away_team_id=None, home_sets_won=3, away_sets_won=0,
            set_scores={"home": [25, 25, 25], "away": [10, 12, 14]},
        ))
        s.add(Schedule(season=SEASON, team_id=a, opponent_team_id=None,
                       opponent_name="Some Junior College", date="2103-09-25",
                       game_time="01:00 PM", site="home"))
    games = client.get("/games", params={"season": SEASON, "date": "2103-09-25"}).json()
    assert len(games) == 1  # the non-D1 stub collapses into the played contest
    assert games[0]["status"] == "played" and games[0]["contest_id"] == "7100012"
