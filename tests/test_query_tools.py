"""Shared query-tools layer (``vb.query.tools``) — the read-only functions the MCP server and the
in-app Ask box both call.

Exercises leaderboard ordering + class/position/conference filters (the *"freshmen with the most
kills"* path), search_players, and the run_tool dispatcher, against a synthetic season. The
season-scope leaderboard reads the ``player_season_stats`` matview, so the fixture refreshes it
after loading game stats.
"""
from __future__ import annotations

import pytest
from conftest import requires_db
from sqlalchemy import text

from vb.db import session_scope
from vb.derive import derive_cumulative
from vb.models import Conference, Contest, Player, PlayerGameStat, Team
from vb.query import tools as qt

pytestmark = requires_db

SEASON = 2104  # sentinel season
CONF_A, CONF_B = "_QT_CONF_A", "_QT_CONF_B"
TEAM_A, TEAM_B = "_QT_TEAM_A", "_QT_TEAM_B"


def _wipe(s):
    s.execute(text("DELETE FROM player_game_stats WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM contests WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM players WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM teams WHERE name LIKE '_QT_TEAM%'"))
    s.execute(text("DELETE FROM conferences WHERE name LIKE '_QT_CONF%'"))


@pytest.fixture
def fixture_ids():
    with session_scope() as s:
        _wipe(s)
    with session_scope() as s:
        ca = Conference(name=CONF_A); cb = Conference(name=CONF_B)
        s.add_all([ca, cb]); s.flush()
        ta = Team(name=TEAM_A, conference_id=ca.id, short_name="_QT A")
        tb = Team(name=TEAM_B, conference_id=cb.id, short_name="_QT B")
        s.add_all([ta, tb]); s.flush()

        # Kills descending: freshman OH (top) > senior MB > freshman S. Class/position spread lets
        # us prove the "freshmen with the most kills" filter path.
        p1 = Player(team_id=ta.id, season=SEASON, name="_QT Frosh OH", position="OH",
                    class_year="Fr", ncaa_player_id="QTP1")
        p2 = Player(team_id=ta.id, season=SEASON, name="_QT Senior MB", position="MB",
                    class_year="Sr", ncaa_player_id="QTP2")
        p3 = Player(team_id=tb.id, season=SEASON, name="_QT Frosh S", position="S",
                    class_year="Fr", ncaa_player_id="QTP3")
        s.add_all([p1, p2, p3]); s.flush()

        s.add(Contest(contest_id="QT_C1", season=SEASON, date="2104-09-06 18:00",
                      home_team_id=ta.id, away_team_id=tb.id))
        s.flush()
        # retatt/rerr = receptions / reception errors → rec_net ("passing"): p3 net 38 > p1 net 25;
        # the MB (p2) has no serve-receive.
        s.add(PlayerGameStat(contest_id="QT_C1", player_id=p1.id, team_id=ta.id, season=SEASON,
                             sets=3, kills=20, aces=4, digs=6, total_attacks=40, retatt=30, rerr=5))
        s.add(PlayerGameStat(contest_id="QT_C1", player_id=p2.id, team_id=ta.id, season=SEASON,
                             sets=3, kills=12, block_solos=3, total_attacks=20))
        s.add(PlayerGameStat(contest_id="QT_C1", player_id=p3.id, team_id=tb.id, season=SEASON,
                             sets=3, kills=5, assists=35, total_attacks=8, retatt=40, rerr=2))
        ids = {"p1": p1.id, "p2": p2.id, "p3": p3.id, "ta": ta.id, "tb": tb.id}
    # The season-scope leaderboard reads the matview; refresh it so the fixture rows appear.
    with session_scope() as s:
        derive_cumulative(s, concurrently=False)
    yield ids
    with session_scope() as s:
        _wipe(s)
    with session_scope() as s:
        derive_cumulative(s, concurrently=False)


def _ours(rows):
    return [r for r in rows if str(r.get("player", "")).startswith("_QT")]


@requires_db
def test_leaderboard_orders_by_stat(fixture_ids):
    with session_scope() as s:
        rows = _ours(qt.leaderboard(s, stat="kills", season=SEASON))
    assert [r["player"] for r in rows] == ["_QT Frosh OH", "_QT Senior MB", "_QT Frosh S"]
    assert rows[0]["value"] == 20.0
    assert rows[0]["rank"] == 1


@requires_db
def test_leaderboard_class_year_filter(fixture_ids):
    """'freshman' matches stored 'Fr' — the headline MCP use case."""
    with session_scope() as s:
        frosh = _ours(qt.leaderboard(s, stat="kills", season=SEASON, class_year="freshman"))
    names = {r["player"] for r in frosh}
    assert names == {"_QT Frosh OH", "_QT Frosh S"}   # senior excluded
    # Top freshman by kills is the OH.
    assert frosh[0]["player"] == "_QT Frosh OH"


@requires_db
def test_leaderboard_rec_net_is_passing_not_assists(fixture_ids):
    """A 'passer' ranks by receptions minus reception errors (serve receive) — not assists.

    The assist leader (Frosh S, 35 assists) must NOT top the passing board; the best net passer does.
    """
    with session_scope() as s:
        rows = _ours(qt.leaderboard(s, stat="rec_net", season=SEASON))
    assert [r["player"] for r in rows[:2]] == ["_QT Frosh S", "_QT Frosh OH"]
    top = rows[0]
    assert top["value"] == 38.0            # 40 receptions - 2 errors
    assert top["receptions"] == 40.0 and top["reception_errors"] == 2.0


@requires_db
def test_leaderboard_position_filter(fixture_ids):
    with session_scope() as s:
        ohs = _ours(qt.leaderboard(s, stat="kills", season=SEASON, position="OH"))
    assert {r["player"] for r in ohs} == {"_QT Frosh OH"}


@requires_db
def test_leaderboard_conference_filter(fixture_ids):
    with session_scope() as s:
        conf_a = _ours(qt.leaderboard(s, stat="kills", season=SEASON, conference=CONF_A))
    assert {r["player"] for r in conf_a} == {"_QT Frosh OH", "_QT Senior MB"}


@requires_db
def test_leaderboard_unknown_stat_returns_error(fixture_ids):
    with session_scope() as s:
        res = qt.leaderboard(s, stat="touchdowns", season=SEASON)
    assert isinstance(res, dict) and "error" in res


@requires_db
def test_search_players_substring(fixture_ids):
    with session_scope() as s:
        hits = qt.search_players(s, query="Frosh", season=SEASON)
    names = {h["player"] for h in hits}
    assert names == {"_QT Frosh OH", "_QT Frosh S"}
    assert all("player_id" in h for h in hits)


@requires_db
def test_run_tool_dispatch_and_unknown(fixture_ids):
    with session_scope() as s:
        rows = qt.run_tool(s, "leaderboard", {"stat": "kills", "season": SEASON})
        assert _ours(rows)[0]["player"] == "_QT Frosh OH"
        # Unknown tool name is reported, not raised.
        assert "error" in qt.run_tool(s, "nope", {})
        # Bad argument surfaces as a structured error, not a crash.
        assert "error" in qt.run_tool(s, "search_players", {"bogus_arg": 1})
