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
from vb.models import Conference, Contest, PbpEvent, Player, PlayerGameStat, Team
from vb.query import tools as qt

pytestmark = requires_db

SEASON = 2104  # sentinel season
CONF_A, CONF_B = "_QT_CONF_A", "_QT_CONF_B"
TEAM_A, TEAM_B = "_QT_TEAM_A", "_QT_TEAM_B"


def _wipe(s):
    s.execute(text("DELETE FROM pbp_events WHERE season = :y"), {"y": SEASON})
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
                             sets=3, kills=20, aces=4, digs=6, total_attacks=40, retatt=30, rerr=5,
                             block_assists=4))
        s.add(PlayerGameStat(contest_id="QT_C1", player_id=p2.id, team_id=ta.id, season=SEASON,
                             sets=3, kills=12, block_solos=3, total_attacks=20))
        s.add(PlayerGameStat(contest_id="QT_C1", player_id=p3.id, team_id=tb.id, season=SEASON,
                             sets=3, kills=5, assists=35, total_attacks=8, retatt=40, rerr=2))

        # Synthetic play-by-play for set 1 of QT_C1 (home=ta, away=tb). Hand-built so the momentum
        # math is checkable: tb takes an early 2-0 run, ta answers with a 4-point run to lead 4-2,
        # tb gets the last point → final 4-3 home. That gives ties=1 (2-2), one lead change
        # (tb-ahead → ta-ahead at 2-3), away biggest run 2, home biggest run 4. r3 has a set touch
        # by p2 before p1's kill → assist p2 (exercises the rally look-back / rally_log).
        def _pbp(seq, rally, touch, player, team, *, term=None, scoring=None, a=0, h=0):
            s.add(PbpEvent(
                contest_id="QT_C1", season=SEASON, set_number=1, rally_number=rally, seq=seq,
                touch_type=touch, player_name=player.name if player else None,
                player_id=player.id if player else None, team_id=team.id if team else None,
                is_terminal=term is not None, terminal_type=term, scoring_team_id=scoring,
                away_score=a, home_score=h,
            ))
        _pbp(1, 1, "terminal", p3, tb, term="ace",   scoring=tb.id, a=1, h=0)
        _pbp(2, 2, "terminal", p3, tb, term="kill",  scoring=tb.id, a=2, h=0)
        _pbp(3, 3, "set",      p2, ta)  # setter for the next kill
        _pbp(4, 3, "terminal", p1, ta, term="kill",  scoring=ta.id, a=2, h=1)
        _pbp(5, 4, "terminal", p1, ta, term="kill",  scoring=ta.id, a=2, h=2)
        _pbp(6, 5, "terminal", p1, ta, term="kill",  scoring=ta.id, a=2, h=3)
        _pbp(7, 6, "terminal", p2, ta, term="block", scoring=ta.id, a=2, h=4)
        _pbp(8, 7, "terminal", p3, tb, term="kill",  scoring=tb.id, a=3, h=4)

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
def test_team_stats_single_team_lookup(fixture_ids):
    """`team=` returns just that team's aggregate line regardless of ranking — the fix for the Ask
    'not in the top 100 teams' failure (a leaderboard-only tool couldn't answer 'X's hitting %')."""
    with session_scope() as s:
        rows = qt.team_stats(s, season=SEASON, team=TEAM_A, sort_by="hit_pct")
    assert len(rows) == 1 and rows[0]["team"] == TEAM_A
    assert rows[0]["kills"] == 32.0  # p1 (20) + p2 (12)
    # Team blocks use NCAA's convention: solo blocks + block assists / 2 (a block assist is credited
    # to every player on the block, so summing per-player totals would double-count it).
    assert rows[0]["total_blocks"] == 5.0  # p2 solos (3) + p1 assists (4) / 2
    with session_scope() as s:
        assert "error" in qt.team_stats(s, season=SEASON, team="__no_such_team__")


@requires_db
def test_team_heights_single_team_lookup(fixture_ids):
    with session_scope() as s:
        rows = qt.team_heights(s, season=SEASON, team=TEAM_A)
    # Fixture players have no recorded height, so TEAM_A has zero measured players → no row.
    assert rows == []
    with session_scope() as s:
        assert "error" in qt.team_heights(s, season=SEASON, team="__no_such_team__")


@requires_db
def test_run_tool_dispatch_and_unknown(fixture_ids):
    with session_scope() as s:
        rows = qt.run_tool(s, "leaderboard", {"stat": "kills", "season": SEASON})
        assert _ours(rows)[0]["player"] == "_QT Frosh OH"
        # Unknown tool name is reported, not raised.
        assert "error" in qt.run_tool(s, "nope", {})
        # Bad argument surfaces as a structured error, not a crash.
        assert "error" in qt.run_tool(s, "search_players", {"bogus_arg": 1})


@requires_db
def test_match_pbp_momentum_summary(fixture_ids):
    """Resolve a match by team + date and check the per-set momentum math + scoring leaders."""
    with session_scope() as s:
        out = qt.match_pbp(s, team=TEAM_A, date="2104-09-06", season=SEASON)
    assert out["contest_id"] == "QT_C1"
    assert out["home_team"] == TEAM_A and out["away_team"] == TEAM_B
    assert len(out["sets"]) == 1
    st = out["sets"][0]
    assert st["away_points"] == 3 and st["home_points"] == 4 and st["winner"] == "home"
    assert st["ties"] == 1 and st["lead_changes"] == 1
    assert st["biggest_run"] == {"away": 2, "home": 4}
    assert st["point_types"]["home"] == {"kills": 3, "aces": 0, "blocks": 1, "opp_errors": 0}
    assert st["point_types"]["away"] == {"kills": 2, "aces": 1, "blocks": 0, "opp_errors": 0}

    leaders = {ld["player"]: ld for ld in out["scoring_leaders"]}
    assert leaders["_QT Frosh OH"]["kills"] == 3 and leaders["_QT Frosh OH"]["points"] == 3
    assert leaders["_QT Frosh S"]["kills"] == 2 and leaders["_QT Frosh S"]["aces"] == 1
    # The MB scored a single block → last of the three scorers.
    assert out["scoring_leaders"][-1]["player"] == "_QT Senior MB"


@requires_db
def test_match_pbp_rally_log_and_assist(fixture_ids):
    """include_rally_log surfaces the point-by-point sequence with the assisting setter."""
    with session_scope() as s:
        out = qt.match_pbp(s, team=TEAM_A, date="2104-09-06", season=SEASON,
                           include_rally_log=True)
    log = out["rally_log"]
    assert len(log) == 7  # one line per scored point
    assert any("assist _QT Senior MB" in line for line in log)


@requires_db
def test_match_pbp_resolution_errors(fixture_ids):
    with session_scope() as s:
        assert "error" in qt.match_pbp(s, team="__no_such_team__", date="2104-09-06", season=SEASON)
        # No game that day → error.
        assert "error" in qt.match_pbp(s, team=TEAM_A, date="2104-09-07", season=SEASON)
        # Dispatches through run_tool too.
        via = qt.run_tool(s, "match_pbp", {"team": TEAM_A, "date": "2104-09-06", "season": SEASON})
        assert via["sets"][0]["home_points"] == 4
