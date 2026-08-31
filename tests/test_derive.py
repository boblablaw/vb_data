"""End-to-end derivation test against Postgres (skipped if the DB is unreachable).

Loads a tiny synthetic fixture into player_game_stats, refreshes the matview, and asserts
the derived cumulative row matches the hand-computed answer.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from vb.db import engine, session_scope
from vb.derive import derive_cumulative
from vb.models import (
    Conference,
    Contest,
    Player,
    PlayerGameStat,
    PlayerSeasonStat,
    Team,
)


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

SEASON = 1900  # sentinel season, unlikely to collide with real data


@pytest.fixture
def _clean():
    """Remove any prior sentinel-season fixture rows before and after the test."""
    def _wipe(s):
        s.execute(text("DELETE FROM player_game_stats WHERE season = :y"), {"y": SEASON})
        s.execute(text("DELETE FROM contests WHERE season = :y"), {"y": SEASON})
        s.execute(text("DELETE FROM players WHERE season = :y"), {"y": SEASON})
        s.execute(text("DELETE FROM teams WHERE name = :n"), {"n": "_TEST_TEAM"})
        s.execute(text("DELETE FROM conferences WHERE name = :n"), {"n": "_TEST_CONF"})
    with session_scope() as s:
        _wipe(s)
    yield
    with session_scope() as s:
        _wipe(s)


@requires_db
def test_derive_matches_hand_computed(_clean):
    with session_scope() as s:
        conf = Conference(name="_TEST_CONF")
        s.add(conf)
        s.flush()
        team = Team(name="_TEST_TEAM", conference_id=conf.id)
        s.add(team)
        s.flush()
        player = Player(team_id=team.id, season=SEASON, name="Test Player",
                        ncaa_player_id="TEST1")
        s.add(player)
        s.flush()
        for cid, sets, kills, errors, ta in [
            ("TESTC_A", 3, 10, 2, 25),
            ("TESTC_B", 4, 6, 4, 20),
        ]:
            s.add(Contest(contest_id=cid, season=SEASON))
            s.flush()
            s.add(PlayerGameStat(
                contest_id=cid, player_id=player.id, team_id=team.id, season=SEASON,
                sets=sets, kills=kills, errors=errors, total_attacks=ta,
            ))
        s.flush()
        pid = player.id

    with session_scope() as s:
        derive_cumulative(s)

    with session_scope() as s:
        row = s.get(PlayerSeasonStat, (pid, SEASON))
        assert row is not None
        assert row.gp == 2
        assert row.sp == 7
        assert row.kills == 16
        assert row.errors == 6
        assert row.total_attacks == 45
        assert abs(row.hit_pct - (10 / 45)) < 1e-6
        assert abs(row.kills_per_set - (16 / 7)) < 1e-6
        assert row.gs is None  # GS has no per-game source
