"""Stats/fantasy API tests against Postgres (skipped if the DB is unreachable).

Loads a synthetic sentinel-season fixture and exercises the new endpoints by calling the
router functions directly with a Session (no TestClient/httpx dependency). Covers:
  * season-anchored week numbering, including null/malformed dates -> the "unknown" bucket,
  * week-scope leaderboard ordering + conference / position / team filters,
  * the Fantasy Points composite (default weights, an override, and COALESCE on an all-null row),
  * unified /search.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select, text

from vb.api.routers.stats import (
    _player_leaderboard,
    fantasy_leaderboard,
    list_weeks,
    search,
)
from vb.config import FANTASY_WEIGHTS
from vb.db import engine, session_scope
from vb.derive import derive_cumulative
from vb.models import Conference, Contest, ContestWeek, Player, PlayerGameStat, Team


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

SEASON = 2101  # far-future sentinel, won't collide with real data
CONF_A, CONF_B = "_ST_CONF_A", "_ST_CONF_B"
TEAM_A, TEAM_B = "_ST_TEAM_A", "_ST_TEAM_B"

BASE = date(2101, 9, 6)  # week-1 anchor; +7d -> week 2, +14d -> week 3


def _dt(d: date) -> str:
    """Terse fixture-date formatter -> 'YYYY-MM-DD 18:00'."""
    return f"{d.isoformat()} 18:00"


def _wipe(s):
    s.execute(text("DELETE FROM player_game_stats WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM contests WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM players WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM teams WHERE name LIKE '_ST_TEAM%'"))
    s.execute(text("DELETE FROM conferences WHERE name LIKE '_ST_CONF%'"))


@pytest.fixture
def fixture_ids():
    """Insert the synthetic season and yield the player/team ids; clean up around the test."""
    with session_scope() as s:
        _wipe(s)
    with session_scope() as s:
        ca = Conference(name=CONF_A); cb = Conference(name=CONF_B)
        s.add_all([ca, cb]); s.flush()
        ta = Team(name=TEAM_A, conference_id=ca.id)
        tb = Team(name=TEAM_B, conference_id=cb.id)
        s.add_all([ta, tb]); s.flush()

        p1 = Player(team_id=ta.id, season=SEASON, name="_ST P1", position="OH", ncaa_player_id="STP1")
        p2 = Player(team_id=ta.id, season=SEASON, name="_ST P2", position="MB", ncaa_player_id="STP2")
        p3 = Player(team_id=tb.id, season=SEASON, name="_ST P3", position="S", ncaa_player_id="STP3")
        p4 = Player(team_id=tb.id, season=SEASON, name="_ST P4", position="DS", ncaa_player_id="STP4")
        s.add_all([p1, p2, p3, p4]); s.flush()

        # Contests: two in week 1 (same date), one +7d (week 2), one +14d (week 3),
        # one null date and one malformed (regex-fail) date -> the unknown bucket.
        contests = {
            "C_W1a": _dt(BASE), "C_W1b": _dt(BASE),
            "C_W2": _dt(BASE + timedelta(days=7)), "C_W3": _dt(BASE + timedelta(days=14)),
            "C_NULL": None, "C_BAD": "TBD-not-a-date",
        }
        for cid, dt in contests.items():
            s.add(Contest(contest_id=cid, season=SEASON, date=dt,
                          home_team_id=ta.id, away_team_id=tb.id))
        s.flush()

        # All player game stats land in week 1 (contest C_W1a).
        s.add(PlayerGameStat(contest_id="C_W1a", player_id=p1.id, team_id=ta.id, season=SEASON,
                             sets=3, kills=15, errors=3, total_attacks=30, assists=1, aces=2,
                             digs=5, block_solos=1, block_assists=2, serr=1, pts=18))
        s.add(PlayerGameStat(contest_id="C_W1a", player_id=p2.id, team_id=ta.id, season=SEASON,
                             sets=3, kills=8, errors=1, total_attacks=15, block_solos=3,
                             block_assists=4, pts=13))
        s.add(PlayerGameStat(contest_id="C_W1a", player_id=p3.id, team_id=tb.id, season=SEASON,
                             sets=3, kills=1, assists=40, digs=3, aces=1, pts=2))
        # P4: every counting column left NULL -> exercises COALESCE.
        s.add(PlayerGameStat(contest_id="C_W1a", player_id=p4.id, team_id=tb.id, season=SEASON))
        s.flush()
        ids = {"p1": p1.id, "p2": p2.id, "p3": p3.id, "p4": p4.id,
               "ta": ta.id, "tb": tb.id, "ca": ca.id, "cb": cb.id}
    yield ids
    with session_scope() as s:
        _wipe(s)


# ---------- week anchoring ----------

@requires_db
def test_week_anchoring_and_unknown_bucket(fixture_ids):
    with session_scope() as s:
        by_cid = dict(s.execute(
            select(ContestWeek.contest_id, ContestWeek.week_number)
            .where(ContestWeek.season == SEASON)
        ).all())
    assert by_cid["C_W1a"] == 1
    assert by_cid["C_W1b"] == 1
    assert by_cid["C_W2"] == 2      # +7 days -> next Mon–Sun week
    assert by_cid["C_W3"] == 3      # +14 days -> two weeks later
    assert by_cid["C_NULL"] is None       # null date -> unknown
    assert by_cid["C_BAD"] is None        # regex-fail date -> unknown


@requires_db
def test_list_weeks_counts(fixture_ids):
    with session_scope() as s:
        weeks = list_weeks(season=SEASON, db=s)
    numbered = {w.week_number: w for w in weeks if w.week_number is not None}
    assert numbered[1].contest_count == 2   # C_W1a + C_W1b
    assert numbered[2].contest_count == 1
    assert numbered[3].contest_count == 1
    assert numbered[1].start is not None and numbered[1].end is not None
    unknown = [w for w in weeks if w.week_number is None]
    assert unknown and unknown[0].contest_count == 2   # C_NULL + C_BAD


# ---------- leaderboards (week scope, live aggregation) ----------

def _lb(s, **kw):
    kw.setdefault("scope", "week"); kw.setdefault("season", SEASON); kw.setdefault("week", 1)
    kw.setdefault("conference", None); kw.setdefault("conference_id", None)
    kw.setdefault("position", None); kw.setdefault("min_sets", 0)
    kw.setdefault("limit", 50); kw.setdefault("offset", 0)
    return _player_leaderboard(s, **kw)


@requires_db
def test_week_leaderboard_ordering(fixture_ids):
    with session_scope() as s:
        rows = _lb(s, stat="kills")
    ours = [r for r in rows if r.name.startswith("_ST")]
    assert [r.value for r in ours][:3] == [15.0, 8.0, 1.0]  # P1 > P2 > P3, P4 (null) last/absent
    assert ours[0].player_id == fixture_ids["p1"]


@requires_db
def test_week_leaderboard_conference_and_position_filters(fixture_ids):
    with session_scope() as s:
        conf = _lb(s, stat="kills", conference=CONF_A)
        pos = _lb(s, stat="kills", position="OH")
        team = _lb(s, stat="kills", team_id=fixture_ids["ta"])
    names_conf = {r.name for r in conf if r.name.startswith("_ST")}
    assert names_conf == {"_ST P1", "_ST P2"}          # conf A only
    names_pos = {r.name for r in pos if r.name.startswith("_ST")}
    assert names_pos == {"_ST P1"}                       # OH only
    names_team = {r.name for r in team if r.name.startswith("_ST")}
    assert names_team == {"_ST P1", "_ST P2"}            # team A roster


@requires_db
def test_min_attacks_qualifier_floors_rate_stats(fixture_ids):
    # P1 has 30 total attacks, P2 has 15; the attempts floor gates the hit% board so a
    # low-volume player can't top it on a lucky swing (P3/P4 have no attacks at all).
    with session_scope() as s:
        floor20 = _lb(s, stat="hit_pct", min_attacks=20)
        floor10 = _lb(s, stat="hit_pct", min_attacks=10)
    assert {r.name for r in floor20 if r.name.startswith("_ST")} == {"_ST P1"}
    assert {r.name for r in floor10 if r.name.startswith("_ST")} == {"_ST P1", "_ST P2"}


# ---------- fantasy composite ----------

def _fantasy(s, weights, **kw):
    kw.setdefault("scope", "week"); kw.setdefault("season", SEASON); kw.setdefault("week", 1)
    kw.setdefault("conference", None); kw.setdefault("conference_id", None)
    kw.setdefault("position", None); kw.setdefault("min_sets", 0)
    kw.setdefault("limit", 50); kw.setdefault("offset", 0)
    return fantasy_leaderboard(db=s, weights=weights, **kw)


@requires_db
def test_fantasy_default_weights_hand_computed(fixture_ids):
    with session_scope() as s:
        rows = _fantasy(s, dict(FANTASY_WEIGHTS))
    p1 = next(r for r in rows if r.player_id == fixture_ids["p1"])
    # 15*1 + 2*1.5 + 5*0.5 + 1*0.25 + 1*1 + 2*0.5 + 3*-0.5 + 1*-0.5 = 20.75
    assert p1.value == pytest.approx(20.75)


@requires_db
def test_fantasy_weight_override_changes_value(fixture_ids):
    weights = dict(FANTASY_WEIGHTS); weights["aces"] = 0.0
    with session_scope() as s:
        rows = _fantasy(s, weights)
    p1 = next(r for r in rows if r.player_id == fixture_ids["p1"])
    assert p1.value == pytest.approx(20.75 - 2 * 1.5)  # aces removed -> 17.75


@requires_db
def test_fantasy_coalesces_all_null_row(fixture_ids):
    with session_scope() as s:
        rows = _fantasy(s, dict(FANTASY_WEIGHTS))
    p4 = next((r for r in rows if r.player_id == fixture_ids["p4"]), None)
    assert p4 is not None and p4.value == pytest.approx(0.0)  # all-null -> 0, not None/error


# ---------- season scope (matview) ----------

@requires_db
def test_season_leaderboard_orders_by_matview(fixture_ids):
    with session_scope() as s:
        derive_cumulative(s)
    with session_scope() as s:
        rows = _player_leaderboard(
            s, stat="kills", scope="season", season=SEASON, week=None,
            conference=None, conference_id=None, position=None, min_sets=0,
            limit=50, offset=0,
        )
    ours = [r for r in rows if r.name.startswith("_ST")]
    assert ours[0].player_id == fixture_ids["p1"]
    assert ours[0].value == 15.0


# ---------- search ----------

@requires_db
def test_search_players_and_teams(fixture_ids):
    with session_scope() as s:
        res = search(q="_ST", season=SEASON, limit=20, db=s)
    assert {p.name for p in res.players} >= {"_ST P1", "_ST P2", "_ST P3", "_ST P4"}
    with session_scope() as s:
        res2 = search(q="_ST_TEAM_A", season=SEASON, limit=20, db=s)
    assert any(t.name == TEAM_A for t in res2.teams)
