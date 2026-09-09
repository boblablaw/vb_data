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
from fastapi import HTTPException
from sqlalchemy import select, text

from vb.api.routers.contests import contest_pbp, contest_stats
from vb.api.routers.players import player_transfer, resolve_player
from vb.api.routers.stats import (
    _player_leaderboard,
    adaptive_qualifier,
    fantasy_leaderboard,
    list_weeks,
    search,
    team_attack_splits,
    team_player_stats,
    team_records,
)
from vb.config import FANTASY_WEIGHTS
from vb.db import engine, session_scope
from vb.derive import derive_cumulative
from vb.models import (
    Conference,
    Contest,
    ContestWeek,
    PbpEvent,
    Player,
    PlayerGameStat,
    Team,
)
from vb.query.tools import match_lineups, per_rotation_stats, transfer_impact


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
    # pbp_events / player_pbp_stats cascade off contests/players, but delete explicitly for clarity.
    s.execute(text("DELETE FROM pbp_events WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM player_pbp_stats WHERE season = :y"), {"y": SEASON})
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
        ta = Team(name=TEAM_A, conference_id=ca.id, short_name="_ST A")
        tb = Team(name=TEAM_B, conference_id=cb.id, short_name="_ST B")
        s.add_all([ta, tb]); s.flush()

        p1 = Player(team_id=ta.id, season=SEASON, name="_ST P1", position="OH",
                    class_year="Sr", ncaa_player_id="STP1", number=12)
        p2 = Player(team_id=ta.id, season=SEASON, name="_ST P2", position="MB",
                    class_year="Jr", ncaa_player_id="STP2")
        p3 = Player(team_id=tb.id, season=SEASON, name="_ST P3", position="S",
                    class_year="So", ncaa_player_id="STP3")
        p4 = Player(team_id=tb.id, season=SEASON, name="_ST P4", position="DS",
                    class_year="Fr", ncaa_player_id="STP4")
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
def test_week_leaderboard_class_year_filter(fixture_ids):
    with session_scope() as s:
        seniors = _lb(s, stat="kills", class_year="Sr")
        juniors = _lb(s, stat="kills", class_year="Jr")
    assert {r.name for r in seniors if r.name.startswith("_ST")} == {"_ST P1"}   # only the Sr
    assert {r.name for r in juniors if r.name.startswith("_ST")} == {"_ST P2"}   # only the Jr


@requires_db
def test_min_attacks_qualifier_floors_rate_stats(fixture_ids):
    # P1 has 30 total attacks, P2 has 15; the attempts floor gates the hit% board so a
    # low-volume player can't top it on a lucky swing (P3/P4 have no attacks at all).
    with session_scope() as s:
        floor20 = _lb(s, stat="hit_pct", min_attacks=20)
        floor10 = _lb(s, stat="hit_pct", min_attacks=10)
    assert {r.name for r in floor20 if r.name.startswith("_ST")} == {"_ST P1"}
    assert {r.name for r in floor10 if r.name.startswith("_ST")} == {"_ST P1", "_ST P2"}


# ---------- adaptive rate-stat qualifier ----------

def _qual(s, **kw):
    kw.setdefault("scope", "week"); kw.setdefault("season", SEASON); kw.setdefault("week", 1)
    kw.setdefault("conference", None); kw.setdefault("conference_id", None)
    kw.setdefault("position", None)
    return adaptive_qualifier(s, **kw)


@requires_db
def test_adaptive_qualifier_scales_and_clamps(fixture_ids):
    # All 4 fixture players appear in exactly one week-1 contest -> p75 games-played anchor = 1.
    # Per-set floor: max(SETS_FLOOR=3, round(2.0*1)=2) = 3, clamped to the field max sets (3).
    # Hit% floor: max(ATTS_FLOOR=10, round(6.67*1)=7) = 10, under the field max attacks (30).
    with session_scope() as s:
        sets_q = _qual(s, stat="kills_per_set")
        att_q = _qual(s, stat="hit_pct")
        none_q = _qual(s, stat="kills")
    assert sets_q == {"by": "sets", "min": 3, "anchor": 1}
    assert att_q == {"by": "attacks", "min": 10, "anchor": 1}
    assert none_q is None  # counting stats need no floor


@requires_db
def test_adaptive_qualifier_season_scope_returns_floor(fixture_ids):
    with session_scope() as s:
        derive_cumulative(s)
    with session_scope() as s:
        q = adaptive_qualifier(
            s, stat="kills_per_set", scope="season", season=SEASON, week=None,
            conference=None, conference_id=None, position=None,
        )
    assert q["by"] == "sets" and q["min"] >= 3 and q["anchor"] >= 1


# ---------- fantasy composite ----------

def _fantasy(s, weights, **kw):
    kw.setdefault("scope", "week"); kw.setdefault("season", SEASON); kw.setdefault("week", 1)
    kw.setdefault("conference", None); kw.setdefault("conference_id", None)
    kw.setdefault("position", None); kw.setdefault("q", None); kw.setdefault("min_sets", 0)
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


@requires_db
def test_fantasy_q_filters_by_player_and_team(fixture_ids):
    with session_scope() as s:
        by_player = _fantasy(s, dict(FANTASY_WEIGHTS), q="_ST P1")
        by_team = _fantasy(s, dict(FANTASY_WEIGHTS), q="_ST_TEAM_A")
    assert {r.name for r in by_player if r.name.startswith("_ST")} == {"_ST P1"}
    # Team A roster (P1, P2) matches on team name; P3/P4 (team B) excluded.
    assert {r.name for r in by_team if r.name.startswith("_ST")} == {"_ST P1", "_ST P2"}


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


# ---------- short names ----------

@requires_db
def test_leaderboard_rows_carry_team_short(fixture_ids):
    with session_scope() as s:
        rows = _lb(s, stat="kills")
    p1 = next(r for r in rows if r.player_id == fixture_ids["p1"])
    assert p1.team == TEAM_A and p1.team_short == "_ST A"


@requires_db
def test_search_matches_short_name(fixture_ids):
    with session_scope() as s:
        res = search(q="_ST A", season=SEASON, limit=20, db=s)
    assert any(t.short_name == "_ST A" and t.name == TEAM_A for t in res.teams)


# ---------- team detail: full roster + per-set ----------

@requires_db
def test_team_player_stats_includes_statless_roster_and_per_set(fixture_ids):
    # A rostered player with NO game stats must still appear on the team table.
    with session_scope() as s:
        s.add(Player(team_id=fixture_ids["ta"], season=SEASON, name="_ST BENCH",
                     position="OH", ncaa_player_id="STBENCH"))
    with session_scope() as s:
        derive_cumulative(s)
    with session_scope() as s:
        rows = team_player_stats(
            team_id=fixture_ids["ta"], scope="season", season=SEASON, week=None,
            weights=dict(FANTASY_WEIGHTS), db=s,
        )
    bench = next((r for r in rows if r.name == "_ST BENCH"), None)
    assert bench is not None                     # statless roster player shown
    assert bench.games is None and bench.kills is None and bench.kills_per_set is None
    # P1: 15 kills over 3 sets -> 5.0 kills/set (matview per-set surfaced).
    p1 = next(r for r in rows if r.player_id == fixture_ids["p1"])
    assert p1.kills_per_set == pytest.approx(5.0)
    assert p1.number == 12                        # jersey number surfaced on the roster line
    assert bench.number is None                   # NULL when unknown


# ---------- play-by-play attack splits (setter filter + FBSO/transition) ----------

def _add_pbp_rallies(s, ids):
    """Insert a tiny hand-computed play-by-play for team A into contest C_W1a.

    Hitter P2 swings twice off setter P1: one first-ball side-out kill (team A receives, P1 sets,
    P2 terminates) and one transition kill (team A serves, B attacks in play, A digs, P1 sets, P2
    terminates). Net for P2 off P1: fbso (1,0,1), transition (1,0,1), overall (2,0,2). The setter
    (P1) never attacks, so it must be pruned from the result.
    """
    ta, tb = ids["ta"], ids["tb"]
    p1, p2, p3, p4 = ids["p1"], ids["p2"], ids["p3"], ids["p4"]
    seq = [0]

    def ev(rally, touch, player, team, *, terminal=False, tt=None, scoring=None):
        seq[0] += 1
        return PbpEvent(
            contest_id="C_W1a", season=SEASON, set_number=1, rally_number=rally, seq=seq[0],
            touch_type=touch, player_id=player, team_id=team, is_terminal=terminal,
            terminal_type=tt, scoring_team_id=scoring,
        )

    s.add_all([
        # R1 — B serves, A sides out on the first ball (P1 sets, P2 kills) -> FBSO kill.
        ev(1, "serve", p3, tb),
        ev(1, "reception", p2, ta),
        ev(1, "set", p1, ta),
        ev(1, "attack", p2, ta, terminal=True, tt="kill", scoring=ta),
        # R2 — A serves; B attacks first ball in play; A digs and counters (P1 sets, P2 kills)
        #      -> transition kill for A.
        ev(2, "serve", p1, ta),
        ev(2, "reception", p3, tb),
        ev(2, "set", p3, tb),
        ev(2, "attack", p4, tb),                                   # B first ball, kept in play
        ev(2, "dig", p2, ta),
        ev(2, "set", p1, ta),
        ev(2, "attack", p2, ta, terminal=True, tt="kill", scoring=ta),
    ])


@requires_db
def test_team_attack_splits_setter_filter_and_pruning(fixture_ids):
    with session_scope() as s:
        _add_pbp_rallies(s, fixture_ids)
    with session_scope() as s:
        rows = team_attack_splits(
            team_id=fixture_ids["ta"], setter_player_id=fixture_ids["p1"], season=SEASON, db=s,
        )
    # Only P2 attacked off P1; the setter (no swings) and everyone else are pruned.
    assert [r.player_id for r in rows] == [fixture_ids["p2"]]
    r = rows[0]
    assert (r.kills, r.errors, r.total_attacks) == (2, 0, 2)
    assert (r.fbso_kills, r.fbso_errors, r.fbso_attacks) == (1, 0, 1)
    assert (r.trans_kills, r.trans_errors, r.trans_attacks) == (1, 0, 1)
    # Overall line == fbso + transition (the two phases partition every attack).
    assert (r.kills, r.errors, r.total_attacks) == (
        r.fbso_kills + r.trans_kills, r.fbso_errors + r.trans_errors,
        r.fbso_attacks + r.trans_attacks,
    )


@requires_db
def test_team_attack_splits_empty_without_setter_match(fixture_ids):
    # No play-by-play at all -> empty list (not an error).
    with session_scope() as s:
        rows = team_attack_splits(
            team_id=fixture_ids["ta"], setter_player_id=fixture_ids["p1"], season=SEASON, db=s,
        )
    assert rows == []


@requires_db
def test_team_player_stats_surfaces_pbp_phase_columns(fixture_ids):
    from vb.derive.pbp import derive_pbp

    with session_scope() as s:
        _add_pbp_rallies(s, fixture_ids)
    with session_scope() as s:
        derive_cumulative(s)
    with session_scope() as s:
        derive_pbp(s, SEASON)
    with session_scope() as s:
        rows = team_player_stats(
            team_id=fixture_ids["ta"], scope="season", season=SEASON, week=None,
            weights=dict(FANTASY_WEIGHTS), db=s,
        )
    p2 = next(r for r in rows if r.player_id == fixture_ids["p2"])
    assert (p2.fbso_kills, p2.fbso_errors, p2.fbso_attacks) == (1, 0, 1)
    assert (p2.trans_kills, p2.trans_errors, p2.trans_attacks) == (1, 0, 1)


@requires_db
def test_team_player_stats_week_scope_surfaces_pbp(fixture_ids):
    # Week scope has no batch player_pbp_stats; the advanced (pbp-derived) columns must be filled by
    # a live replay of the week's play-by-play. C_W1a is week 1 (see fixture).
    with session_scope() as s:
        _add_pbp_rallies(s, fixture_ids)
    with session_scope() as s:
        rows = team_player_stats(
            team_id=fixture_ids["ta"], scope="week", season=SEASON, week=1,
            weights=dict(FANTASY_WEIGHTS), db=s,
        )
    p2 = next(r for r in rows if r.player_id == fixture_ids["p2"])
    assert (p2.fbso_kills, p2.fbso_errors, p2.fbso_attacks) == (1, 0, 1)
    assert (p2.trans_kills, p2.trans_errors, p2.trans_attacks) == (1, 0, 1)
    # Setter P1 set twice and served once this week (live set/serve touch counts).
    p1 = next(r for r in rows if r.player_id == fixture_ids["p1"])
    assert p1.set_attempts == 2
    assert p1.serve_attempts == 1


@requires_db
def test_team_attack_splits_week_and_contest_scope(fixture_ids):
    with session_scope() as s:
        _add_pbp_rallies(s, fixture_ids)
    # Narrowed to week 1 -> same result as the whole season (all PBP is in C_W1a).
    with session_scope() as s:
        wk = team_attack_splits(
            team_id=fixture_ids["ta"], setter_player_id=fixture_ids["p1"], season=SEASON,
            week=1, db=s,
        )
    assert [r.player_id for r in wk] == [fixture_ids["p2"]]
    assert (wk[0].fbso_attacks, wk[0].trans_attacks) == (1, 1)
    # Narrowed to a single contest.
    with session_scope() as s:
        cg = team_attack_splits(
            team_id=fixture_ids["ta"], setter_player_id=fixture_ids["p1"], season=SEASON,
            contest_id="C_W1a", db=s,
        )
    assert [r.player_id for r in cg] == [fixture_ids["p2"]]
    # setter_player_id omitted -> every setter; only team A's hitter P2 (P4 is team B) survives.
    with session_scope() as s:
        allset = team_attack_splits(
            team_id=fixture_ids["ta"], setter_player_id=None, season=SEASON, db=s,
        )
    assert [r.player_id for r in allset] == [fixture_ids["p2"]]
    assert (allset[0].kills, allset[0].total_attacks) == (2, 2)


@requires_db
def test_contest_stats_surfaces_fbso_trans(fixture_ids):
    with session_scope() as s:
        _add_pbp_rallies(s, fixture_ids)
    with session_scope() as s:
        rows = contest_stats(contest_id="C_W1a", db=s)
    p2 = next(r for r in rows if r.player_id == fixture_ids["p2"])
    assert (p2.fbso_kills, p2.fbso_errors, p2.fbso_attacks) == (1, 0, 1)
    assert (p2.trans_kills, p2.trans_errors, p2.trans_attacks) == (1, 0, 1)


@requires_db
def test_contest_stats_surfaces_player_bio(fixture_ids):
    # The box-score Pos/Cls/Ht columns read position/class_year/height_inches off each row.
    with session_scope() as s:
        rows = contest_stats(contest_id="C_W1a", db=s)
    p1 = next(r for r in rows if r.player_id == fixture_ids["p1"])
    assert p1.class_year == "Sr"      # newly surfaced by the endpoint (was previously omitted)
    assert p1.number == 12
    p3 = next(r for r in rows if r.player_id == fixture_ids["p3"])
    assert p3.class_year == "So"


# ---------- cross-season player resolve ----------

@requires_db
def test_resolve_player_bridges_seasons_by_identity(fixture_ids):
    # NCAA reissues both player_id and ncaa_player_id every season, so resolving must key on durable
    # identity (name + hometown + high_school), not the season-scoped ncaa id.
    prev = SEASON - 1
    with session_scope() as s:
        # Give the fixture's P1 a durable identity in the current season...
        p1 = s.get(Player, fixture_ids["p1"])
        p1.hometown = "_ST Town"; p1.high_school = "_ST HS"
        # ...and insert the SAME person in the prior season with a *different* ncaa id + player id.
        prior = Player(team_id=fixture_ids["ta"], season=prev, name="_ST P1", position="OH",
                       class_year="Jr", ncaa_player_id="STP1_PREV",
                       hometown="_ST Town", high_school="_ST HS")
        s.add(prior); s.flush()
        prior_id = prior.id
    try:
        # Called directly (not via FastAPI), so every Query-defaulted arg must be passed explicitly.
        with session_scope() as s:
            # The current-season ncaa id must NOT resolve into the prior season...
            with pytest.raises(HTTPException):
                resolve_player(season=prev, ncaa_player_id="STP1", name=None,
                               hometown=None, high_school=None, team_id=None, db=s)
            # ...but identity does, landing on the prior-season row.
            got = resolve_player(season=prev, ncaa_player_id=None, name="_ST P1",
                                 hometown="_ST Town", high_school="_ST HS",
                                 team_id=fixture_ids["ta"], db=s)
            assert got.id == prior_id and got.season == prev
    finally:
        with session_scope() as s:
            s.execute(text("DELETE FROM players WHERE season = :y"), {"y": prev})


# ---------- team records (standings) ----------

@requires_db
def test_team_records_endpoint_derives_wins_from_linescore(fixture_ids):
    # Give two fixture contests a result: TEAM_A (home) wins C_W1a 3-1, loses C_W2 0-3.
    with session_scope() as s:
        c1 = s.get(Contest, "C_W1a"); c1.home_sets_won = 3; c1.away_sets_won = 1
        c2 = s.get(Contest, "C_W2"); c2.home_sets_won = 0; c2.away_sets_won = 3
    with session_scope() as s:
        rows = team_records(season=SEASON, conference=None, conference_id=None, db=s)
    by = {r.team_id: r for r in rows}
    ta, tb = by[fixture_ids["ta"]], by[fixture_ids["tb"]]
    assert (ta.games, ta.wins, ta.losses) == (2, 1, 1)
    assert (ta.sets_won, ta.sets_lost) == (3, 4)          # 3+0 won, 1+3 lost
    assert ta.win_streak == -1                              # most recent (C_W2) was a loss
    # TEAM_A (conf A) only faced TEAM_B (conf B) -> everything is non-conference.
    assert (ta.conf_wins, ta.conf_losses) == (0, 0)
    assert (ta.nonconf_wins, ta.nonconf_losses) == (1, 1)
    assert (tb.wins, tb.losses) == (1, 1) and tb.win_streak == 1


# ---------- search ----------

@requires_db
def test_search_players_and_teams(fixture_ids):
    with session_scope() as s:
        res = search(q="_ST", season=SEASON, limit=20, db=s)
    assert {p.name for p in res.players} >= {"_ST P1", "_ST P2", "_ST P3", "_ST P4"}
    with session_scope() as s:
        res2 = search(q="_ST_TEAM_A", season=SEASON, limit=20, db=s)
    assert any(t.name == TEAM_A for t in res2.teams)


# ---------- transfer / previous school ----------

@pytest.fixture
def transfer_ids():
    """Two seasons of the same person: SEASON-1 at TEAM_B, SEASON at TEAM_A (a transfer in),
    plus a returning player (same team both years) and a true-freshman-style newcomer with no
    prior-season row. Cleans up both seasons around the test."""
    prior = SEASON - 1

    def _wipe_transfer(s):
        # Order matters: stats/contests reference players & teams by FK.
        s.execute(text("DELETE FROM player_game_stats WHERE season IN (:a, :b)"),
                  {"a": prior, "b": SEASON})
        s.execute(text("DELETE FROM contests WHERE season IN (:a, :b) AND contest_id LIKE '_TR_%'"),
                  {"a": prior, "b": SEASON})
        s.execute(text("DELETE FROM players WHERE season IN (:a, :b)"), {"a": prior, "b": SEASON})
        s.execute(text("DELETE FROM teams WHERE name LIKE '_TR_TEAM%'"))
        s.execute(text("DELETE FROM conferences WHERE name LIKE '_TR_CONF%'"))

    with session_scope() as s:
        _wipe_transfer(s)
    with session_scope() as s:
        ca = Conference(name="_TR_CONF_A"); cb = Conference(name="_TR_CONF_B")
        s.add_all([ca, cb]); s.flush()
        ta = Team(name="_TR_TEAM_A", conference_id=ca.id, short_name="_TR A")
        tb = Team(name="_TR_TEAM_B", conference_id=cb.id, short_name="_TR B")
        s.add_all([ta, tb]); s.flush()

        # Prior season: the transfer-to-be at TEAM_B, and the returner at TEAM_A.
        s.add(Player(team_id=tb.id, season=prior, name="_TR Mover", ncaa_player_id="TRM0",
                     hometown="Indianapolis, IN", high_school="Herron"))
        s.add(Player(team_id=ta.id, season=prior, name="_TR Stayer", ncaa_player_id="TRS0",
                     hometown="Chicago, IL", high_school="Whitney"))
        s.flush()

        # Current season: mover now at TEAM_A (transfer), stayer still at TEAM_A, plus a newcomer.
        mover = Player(team_id=ta.id, season=SEASON, name="_TR Mover", ncaa_player_id="TRM1",
                       hometown="Indianapolis, IN", high_school="Herron")
        stayer = Player(team_id=ta.id, season=SEASON, name="_TR Stayer", ncaa_player_id="TRS1",
                        hometown="Chicago, IL", high_school="Whitney")
        newcomer = Player(team_id=tb.id, season=SEASON, name="_TR Frosh", ncaa_player_id="TRF1",
                          hometown="Austin, TX", high_school="Anderson")
        s.add_all([mover, stayer, newcomer]); s.flush()
        ids = {"mover": mover.id, "stayer": stayer.id, "newcomer": newcomer.id,
               "ta": ta.id, "tb": tb.id}
    yield ids
    with session_scope() as s:
        _wipe_transfer(s)


@requires_db
def test_transfer_detects_previous_school(transfer_ids):
    with session_scope() as s:
        out = player_transfer(player_id=transfer_ids["mover"], db=s)
    assert out.transferred is True
    assert out.previous_season == SEASON - 1
    assert out.previous_team_id == transfer_ids["tb"]
    assert out.previous_team == "_TR_TEAM_B"
    assert out.previous_team_short == "_TR B"


@requires_db
def test_transfer_returning_player_is_not_a_transfer(transfer_ids):
    with session_scope() as s:
        out = player_transfer(player_id=transfer_ids["stayer"], db=s)
    assert out.transferred is False


@requires_db
def test_transfer_newcomer_without_prior_row_is_not_a_transfer(transfer_ids):
    with session_scope() as s:
        out = player_transfer(player_id=transfer_ids["newcomer"], db=s)
    assert out.transferred is False


@requires_db
def test_transfer_impact_ranks_new_team_production(transfer_ids):
    # Give the mover a real box score this season (12 sets), then derive the matview.
    with session_scope() as s:
        s.add(Contest(contest_id="_TR_C1", season=SEASON, date=_dt(BASE),
                      home_team_id=transfer_ids["ta"], away_team_id=transfer_ids["tb"]))
        s.flush()
        s.add(PlayerGameStat(contest_id="_TR_C1", player_id=transfer_ids["mover"],
                             team_id=transfer_ids["ta"], season=SEASON,
                             sets=12, kills=48, errors=6, total_attacks=100, assists=6,
                             aces=6, digs=24, block_solos=2, block_assists=4, pts=54))
        s.flush()
        derive_cumulative(s)

    with session_scope() as s:
        rows = transfer_impact(s, season=SEASON, min_sets=10, limit=25)
    names = {r["player"] for r in rows}
    assert "_TR Mover" in names                 # transferred in AND cleared min_sets
    assert "_TR Stayer" not in names            # returning player, not a transfer
    assert "_TR Frosh" not in names             # no prior-season row
    mover = next(r for r in rows if r["player"] == "_TR Mover")
    assert mover["previous_team"] == "_TR_TEAM_B"
    assert mover["previous_team_short"] == "_TR B"
    assert mover["sp"] == 12
    assert mover["pts_per_set"] == round(54 / 12, 2)

    # min_sets qualifier excludes the mover once the floor exceeds their 12 sets.
    with session_scope() as s:
        rows_hi = transfer_impact(s, season=SEASON, min_sets=20, limit=25)
    assert "_TR Mover" not in {r["player"] for r in rows_hi}


# ---------- match lineups (per-set starters / subs from the pbp sub log) ----------

@requires_db
def test_match_lineups_detects_starter_change(fixture_ids):
    ta, tb = fixture_ids["ta"], fixture_ids["tb"]
    with session_scope() as s:
        extra = [Player(team_id=ta, season=SEASON, name=f"_LU A{i}", ncaa_player_id=f"LUA{i}")
                 for i in range(1, 9)]
        s.add_all(extra); s.flush()
        a = [p.id for p in extra]
        lu_day = BASE + timedelta(days=21)  # unique date; fixture contests sit on BASE..+14
        s.add(Contest(contest_id="C_LU", season=SEASON, date=_dt(lu_day),
                      home_team_id=ta, away_team_id=tb))
        s.flush()
        seq = [0]

        def ev(setn, touch, pid):
            seq[0] += 1
            return PbpEvent(contest_id="C_LU", season=SEASON, set_number=setn, rally_number=1,
                            seq=seq[0], touch_type=touch, player_id=pid, team_id=ta)

        rows = []
        # Set 1: A1..A7 on court at the opening (each swings), A8 comes off the bench.
        rows += [ev(1, "attack", pid) for pid in a[:7]]
        rows.append(ev(1, "sub_in", a[7]))
        # Set 2: A8 now starts in place of A7 (A7's first action this set is a sub_in).
        rows += [ev(2, "attack", pid) for pid in a[:6] + [a[7]]]
        rows.append(ev(2, "sub_in", a[6]))
        s.add_all(rows)

    with session_scope() as s:
        out = match_lineups(s, team=TEAM_A, date=lu_day.isoformat(), season=SEASON)

    team = out["teams"][TEAM_A]
    assert team["side"] == "home"
    by_set = {x["set_number"]: x for x in team["sets"]}
    assert {e["player"] for e in by_set[1]["starters"]} == {f"_LU A{i}" for i in range(1, 8)}
    assert {e["player"] for e in by_set[1]["subs"]} == {"_LU A8"}
    assert {e["player"] for e in by_set[2]["starters"]} == (
        {f"_LU A{i}" for i in range(1, 7)} | {"_LU A8"})
    assert team["starters_changed"] is True
    change = next(c for c in team["starter_changes"] if c["set_number"] == 2)
    assert change["added"] == ["_LU A8"]
    assert change["removed"] == ["_LU A7"]


@requires_db
def test_match_lineups_same_starters_reports_no_change(fixture_ids):
    ta, tb = fixture_ids["ta"], fixture_ids["tb"]
    with session_scope() as s:
        extra = [Player(team_id=ta, season=SEASON, name=f"_LU B{i}", ncaa_player_id=f"LUB{i}")
                 for i in range(1, 7)]
        s.add_all(extra); s.flush()
        a = [p.id for p in extra]
        lu_day = BASE + timedelta(days=21)
        s.add(Contest(contest_id="C_LU2", season=SEASON, date=_dt(lu_day),
                      home_team_id=ta, away_team_id=tb))
        s.flush()
        seq = [0]
        rows = []
        for setn in (1, 2):
            for pid in a:
                seq[0] += 1
                rows.append(PbpEvent(contest_id="C_LU2", season=SEASON, set_number=setn,
                                     rally_number=1, seq=seq[0], touch_type="attack",
                                     player_id=pid, team_id=ta))
        s.add_all(rows)
    with session_scope() as s:
        out = match_lineups(s, team=TEAM_A, date=lu_day.isoformat(), season=SEASON)
    team = out["teams"][TEAM_A]
    assert team["starters_changed"] is False
    assert team["starter_changes"] == []


@requires_db
def test_match_lineups_starter_subbed_out_and_back_in_is_still_a_starter(fixture_ids):
    """Regression: a two-setter swap. The starting setter is subbed out and back in within the set;
    the old 'touched but never subbed in' rule dropped her, leaving the setter slot empty. She must
    stay a starter. The libero (a pre-serve, rally-0 sub_in) is the 7th starter; the backup setter
    who enters mid-set is a bench sub."""
    ta, tb = fixture_ids["ta"], fixture_ids["tb"]
    with session_scope() as s:
        extra = [Player(team_id=ta, season=SEASON, name=f"_SW A{i}",
                        position=("L" if i == 7 else "OH"), ncaa_player_id=f"SWA{i}")
                 for i in range(1, 9)]
        s.add_all(extra); s.flush()
        a = [p.id for p in extra]
        sw_day = BASE + timedelta(days=22)
        s.add(Contest(contest_id="C_SW", season=SEASON, date=_dt(sw_day),
                      home_team_id=ta, away_team_id=tb))
        s.flush()
        seq = [0]

        def ev(touch, pid, rally):
            seq[0] += 1
            return PbpEvent(contest_id="C_SW", season=SEASON, set_number=1, rally_number=rally,
                            seq=seq[0], touch_type=touch, player_id=pid, team_id=ta)

        rows = [
            ev("sub_in", a[6], 0),      # libero enters pre-serve -> 7th starter
            *[ev("attack", pid, 1) for pid in a[:6]],  # A1..A6 on court at the opening
            ev("sub_out", a[0], 3),     # starting setter A1 rotates out...
            ev("sub_in", a[7], 3),      # ...backup setter A8 comes off the bench
            ev("sub_out", a[7], 6),     # A8 back out...
            ev("sub_in", a[0], 6),      # ...A1 (the starter) returns
            ev("set", a[0], 7),
        ]
        s.add_all(rows)

    with session_scope() as s:
        out = match_lineups(s, team=TEAM_A, date=sw_day.isoformat(), season=SEASON)

    team = out["teams"][TEAM_A]
    by_set = {x["set_number"]: x for x in team["sets"]}
    starters = {e["player"] for e in by_set[1]["starters"]}
    subs = {e["player"] for e in by_set[1]["subs"]}
    assert "_SW A1" in starters           # subbed out AND back in — still a starter
    assert starters == {f"_SW A{i}" for i in range(1, 8)}   # six + libero = 7
    assert subs == {"_SW A8"}             # the mid-set entrant is a bench sub


@requires_db
def test_match_lineups_defensive_sub_does_not_displace_the_starter(fixture_ids):
    """Regression: two pre-serve defensive subs. A rotation starter (A6) is covered in the back row
    by a NON-libero bench player (A8) who subs in at rally 0 — like a second libero, but a regular
    substitution. The rotation starter A6 stays a STARTER (via her rally-0 sub_out); the bench player
    A8 who covers her is a SUB, not a starter. Only the true libero (A7, position L) entering pre-serve
    counts as the 7th starter. Without this, the count wrongly reads 8."""
    ta, tb = fixture_ids["ta"], fixture_ids["tb"]
    with session_scope() as s:
        extra = [Player(team_id=ta, season=SEASON, name=f"_DS A{i}",
                        position=("L" if i == 7 else "OH"), ncaa_player_id=f"DSA{i}")
                 for i in range(1, 9)]
        s.add_all(extra); s.flush()
        a = [p.id for p in extra]
        ds_day = BASE + timedelta(days=24)
        s.add(Contest(contest_id="C_DS", season=SEASON, date=_dt(ds_day),
                      home_team_id=ta, away_team_id=tb))
        s.flush()
        seq = [0]

        def ev(touch, pid, rally):
            seq[0] += 1
            return PbpEvent(contest_id="C_DS", season=SEASON, set_number=1, rally_number=rally,
                            seq=seq[0], touch_type=touch, player_id=pid, team_id=ta)

        rows = [
            ev("sub_in", a[6], 0),                       # A7 libero (L) -> 7th starter
            ev("sub_out", a[4], 0),                      # A5 covered by the libero -> starter
            ev("sub_in", a[7], 0),                       # A8 (OH) defensive sub, pre-serve -> SUB
            ev("sub_out", a[5], 0),                      # A6 covered by A8 -> still a starter
            *[ev("attack", pid, 1) for pid in a[:4]],    # A1..A4 on court at the opening
        ]
        s.add_all(rows)

    with session_scope() as s:
        out = match_lineups(s, team=TEAM_A, date=ds_day.isoformat(), season=SEASON)

    team = out["teams"][TEAM_A]
    by_set = {x["set_number"]: x for x in team["sets"]}
    starters = {e["player"] for e in by_set[1]["starters"]}
    subs = {e["player"] for e in by_set[1]["subs"]}
    assert starters == {f"_DS A{i}" for i in range(1, 8)}   # A1..A6 rotation + A7 libero = 7
    assert "_DS A6" in starters            # covered by a defensive sub, but still a starter
    assert subs == {"_DS A8"}              # the non-libero pre-serve entrant is a bench sub


@requires_db
def test_contest_pbp_endpoint_includes_per_set_lineups(fixture_ids):
    """/contests/{id}/pbp carries per-set lineups (shared with match_lineups) so the UI needs no
    extra fetch. A set-2 starter change surfaces via starters_changed."""
    ta, tb = fixture_ids["ta"], fixture_ids["tb"]
    with session_scope() as s:
        extra = [Player(team_id=ta, season=SEASON, name=f"_PB A{i}", number=i, position="S",
                        ncaa_player_id=f"PBA{i}") for i in range(1, 9)]
        s.add_all(extra); s.flush()
        a = [p.id for p in extra]
        s.add(Contest(contest_id="C_PBP", season=SEASON, date=_dt(BASE + timedelta(days=23)),
                      home_team_id=ta, away_team_id=tb))
        s.flush()
        seq = [0]

        def ev(setn, touch, pid):
            seq[0] += 1
            return PbpEvent(contest_id="C_PBP", season=SEASON, set_number=setn, rally_number=1,
                            seq=seq[0], touch_type=touch, player_id=pid, team_id=ta)

        rows = [ev(1, "attack", pid) for pid in a[:7]]
        rows.append(ev(1, "sub_in", a[7]))
        rows += [ev(2, "attack", pid) for pid in a[:6] + [a[7]]]   # A8 starts set 2 for A7
        rows.append(ev(2, "sub_in", a[6]))
        s.add_all(rows)

    with session_scope() as s:
        out = contest_pbp("C_PBP", s)

    home = next(t for t in out.lineups if t.side == "home")
    assert home.team_id == ta
    by_set = {ls.set_number: ls for ls in home.sets}
    assert {p.player for p in by_set[1].starters} == {f"_PB A{i}" for i in range(1, 8)}
    # roster fields (number/position) flow through for the UI
    assert all(p.number is not None for p in by_set[1].starters)
    assert home.starters_changed is True
    change = next(c for c in home.starter_changes if c.set_number == 2)
    assert change.added == ["_PB A8"] and change.removed == ["_PB A7"]


# --------------------------------------------------------------------------- per-rotation stats

def _rot_events():
    """Synthetic one-set rally log (away=1 serves first, home=2). No DB needed.

    away setter = player 10, who serves only once away has rotated to positional idx 2 — so the
    displayed R1 for away must map to that positional slot, not the first server. Sequence:
      r1 away serve (p11) -> away kill (away holds)
      r2 away serve (p11) -> home wins (home sides out -> home rotates)
      r3 home serve (p21) -> away wins (away sides out -> away rotates to idx2)
      r4 away serve (p10, the setter) -> away holds
      r5 away serve (p10) -> home wins (home sides out -> home rotates)
    """
    ev = []

    def add(rally, touch, team, pid=None, *, terminal=False, tt=None, scorer=None):
        ev.append(PbpEvent(
            contest_id="X", season=SEASON, set_number=1, rally_number=rally, seq=len(ev) + 1,
            touch_type=touch, team_id=team, player_id=pid,
            is_terminal=terminal, terminal_type=tt, scoring_team_id=scorer,
        ))

    # r1: away serve, away attacks & kills
    add(1, "serve", 1, 11); add(1, "reception", 2, 21)
    add(1, "attack", 1, 11); add(1, "terminal", 1, 11, terminal=True, tt="kill", scorer=1)
    # r2: away serve, home wins (kill)
    add(2, "serve", 1, 11); add(2, "reception", 2, 21)
    add(2, "attack", 2, 22); add(2, "terminal", 2, 22, terminal=True, tt="kill", scorer=2)
    # r3: home serve, away wins (kill)
    add(3, "serve", 2, 21); add(3, "reception", 1, 12)
    add(3, "attack", 1, 12); add(3, "terminal", 1, 12, terminal=True, tt="kill", scorer=1)
    # r4: away serve (setter #10), away holds (ace)
    add(4, "serve", 1, 10); add(4, "terminal", 1, 10, terminal=True, tt="ace", scorer=1)
    # r5: away serve (setter #10), home wins
    add(5, "serve", 1, 10); add(5, "reception", 2, 21)
    add(5, "attack", 2, 22); add(5, "terminal", 2, 22, terminal=True, tt="kill", scorer=2)
    # a setter run needs the most set touches to be identified as the setter
    add(4, "set", 1, 10); add(2, "set", 1, 10); add(1, "set", 1, 10)
    return ev


def test_per_rotation_stats_anchors_r1_to_the_setter():
    out = per_rotation_stats(_rot_events(), 1, 2, {1: "Away", 2: "Home"})
    away = out["Away"]
    assert away["side"] == "away"
    tot = {r["rotation"]: r for r in away["totals"]}
    assert set(tot) == set(range(1, 7))  # always six rotations

    # away setter (#10) served from positional idx 2 -> that slot is displayed as R1.
    assert tot[1]["serve_rallies"] == 2 and tot[1]["serve_won"] == 1   # r4 hold, r5 lost
    assert tot[1]["points_won"] == 1 and tot[1]["points_lost"] == 1
    # the first-served positional slot (idx 1) lands at R6 under the setter anchor.
    assert tot[6]["serve_rallies"] == 2 and tot[6]["serve_won"] == 1   # r1 hold, r2 lost
    assert tot[6]["recv_rallies"] == 1 and tot[6]["recv_won"] == 1     # r3 sideout
    # both away kills (r1 hold, r3 sideout) land at idx 1 -> displayed R6 (away rotates after r3)
    assert tot[6]["points_won"] == 2 and tot[6]["kills"] == 2 and tot[6]["attack_attempts"] == 2


def test_per_rotation_stats_totals_sum_the_per_set_buckets_and_balance():
    ev = _rot_events()
    out = per_rotation_stats(ev, 1, 2, {1: "Away", 2: "Home"})
    for team in out.values():
        # totals == elementwise sum of the per-set rotation buckets
        acc = {r: {} for r in range(1, 7)}
        for s in team["sets"]:
            for r in s["rotations"]:
                for k, v in r.items():
                    if k != "rotation":
                        acc[r["rotation"]][k] = acc[r["rotation"]].get(k, 0) + v
        for t in team["totals"]:
            assert acc[t["rotation"]] == {k: v for k, v in t.items() if k != "rotation"}
        # every rally is counted once per side: serve_rallies + recv_rallies == 5 rallies
        rallies = sum(t["serve_rallies"] + t["recv_rallies"] for t in team["totals"])
        assert rallies == 5
    # points won by away + points won by home == total rallies (each rally has one winner)
    won = sum(t["points_won"] for team in out.values() for t in team["totals"])
    assert won == 5


def test_per_rotation_stats_ignores_subs_and_pre_serve_rows():
    base = per_rotation_stats(_rot_events(), 1, 2, {1: "Away", 2: "Home"})
    noisy = _rot_events()
    # a pre-serve (rally 0) sub and a mid-rally sub_in must not shift rotations or counts
    noisy.append(PbpEvent(contest_id="X", season=SEASON, set_number=1, rally_number=0,
                          seq=999, touch_type="sub_in", team_id=1, player_id=77))
    noisy.append(PbpEvent(contest_id="X", season=SEASON, set_number=1, rally_number=3,
                          seq=998, touch_type="sub_in", team_id=1, player_id=78))
    assert per_rotation_stats(noisy, 1, 2, {1: "Away", 2: "Home"}) == base


@requires_db
def test_contest_pbp_endpoint_includes_rotations(fixture_ids):
    """/contests/{id}/pbp carries per-rotation stats (six rotations per set, totals = sum)."""
    ta, tb = fixture_ids["ta"], fixture_ids["tb"]
    with session_scope() as s:
        s.add(Contest(contest_id="C_ROT", season=SEASON, date=_dt(BASE + timedelta(days=25)),
                      home_team_id=ta, away_team_id=tb))
        s.flush()
        rows = []
        for i, e in enumerate(_rot_events()):
            # remap the synthetic team ids (1->home ta, 2->away tb) onto the fixture teams
            tid = ta if e.team_id == 1 else tb
            scorer = None if e.scoring_team_id is None else (ta if e.scoring_team_id == 1 else tb)
            rows.append(PbpEvent(contest_id="C_ROT", season=SEASON, set_number=e.set_number,
                                 rally_number=e.rally_number, seq=i + 1, touch_type=e.touch_type,
                                 team_id=tid, player_id=e.player_id, is_terminal=e.is_terminal,
                                 terminal_type=e.terminal_type, scoring_team_id=scorer))
        s.add_all(rows)

    with session_scope() as s:
        out = contest_pbp("C_ROT", s)

    assert len(out.rotations) == 2
    home = next(t for t in out.rotations if t.side == "home")  # ta, the synthetic "away" side
    assert [r.rotation for r in home.totals] == [1, 2, 3, 4, 5, 6]
    for ls in home.sets:
        assert [r.rotation for r in ls.rotations] == [1, 2, 3, 4, 5, 6]
    assert sum(r.serve_rallies + r.recv_rallies for r in home.totals) == 5
