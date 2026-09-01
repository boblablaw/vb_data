"""DB-backed tests for load_coaches (skipped if Postgres is unreachable).

Verifies the NCAA head-coach CSV loads into the coaches table, that loading REPLACES the season's
existing coaches (evicting legacy teams.json rows), that unmatched team ids are skipped, and that a
missing CSV raises.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from vb.db import engine, session_scope
from vb.load.coaches import load_coaches
from vb.models import Coach, Conference, Team, TeamSeasonId


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

SEASON = 1900  # sentinel season, unlikely to collide with real data
NCAA_ID = "TESTCOACH_TID"


@pytest.fixture
def _team():
    """Create a sentinel team with a season id; clean coaches/team/conf before and after."""
    def _wipe(s):
        s.execute(text("DELETE FROM coaches WHERE season = :y"), {"y": SEASON})
        s.execute(text("DELETE FROM team_season_ids WHERE season = :y"), {"y": SEASON})
        s.execute(text("DELETE FROM teams WHERE name = :n"), {"n": "_TEST_TEAM"})
        s.execute(text("DELETE FROM conferences WHERE name = :n"), {"n": "_TEST_CONF"})
    with session_scope() as s:
        _wipe(s)
    with session_scope() as s:
        conf = Conference(name="_TEST_CONF")
        s.add(conf)
        s.flush()
        team = Team(name="_TEST_TEAM", conference_id=conf.id)
        s.add(team)
        s.flush()
        s.add(TeamSeasonId(team_id=team.id, season=SEASON, ncaa_team_id=NCAA_ID))
        s.flush()
        tid = team.id
    yield tid
    with session_scope() as s:
        _wipe(s)


def _write_csv(path, rows):
    header = "TeamID,Team,CoachName,CoachId,Seasons,Record\n"
    path.write_text(header + "".join(",".join(r) + "\n" for r in rows))
    return path


@requires_db
def test_loads_head_coach_with_ncaa_fields(_team, tmp_path):
    csv = _write_csv(tmp_path / "c.csv",
                     [[NCAA_ID, "_TEST_TEAM", "Jane Smith", "998877", "5th", "120-45"]])
    with session_scope() as s:
        res = load_coaches(s, SEASON, csv)
    assert res == {"coaches": 1, "skipped": 0}
    with session_scope() as s:
        rows = s.query(Coach).filter(Coach.team_id == _team, Coach.season == SEASON).all()
        assert len(rows) == 1
        c = rows[0]
        assert (c.name, c.title, c.ncaa_coach_id, c.seasons, c.record) == \
            ("Jane Smith", "Head Coach", "998877", "5th", "120-45")


@requires_db
def test_replaces_existing_season_coaches(_team, tmp_path):
    # A legacy teams.json-style assistant already in the table for this season...
    with session_scope() as s:
        s.add(Coach(team_id=_team, season=SEASON, name="Old Assistant",
                    title="Assistant Coach", email="a@x.edu"))
    csv = _write_csv(tmp_path / "c.csv",
                     [[NCAA_ID, "_TEST_TEAM", "Jane Smith", "998877", "5th", "120-45"]])
    with session_scope() as s:
        load_coaches(s, SEASON, csv)
    with session_scope() as s:
        rows = s.query(Coach).filter(Coach.season == SEASON).all()
        assert [r.name for r in rows] == ["Jane Smith"]  # assistant evicted


@requires_db
def test_skips_unmatched_team_id(_team, tmp_path):
    csv = _write_csv(tmp_path / "c.csv",
                     [["NO_SUCH_TID", "Ghost", "Nobody", "1", "1st", "0-0"]])
    with session_scope() as s:
        res = load_coaches(s, SEASON, csv)
    assert res == {"coaches": 0, "skipped": 1}


@requires_db
def test_missing_csv_raises(_team, tmp_path):
    with session_scope() as s, pytest.raises(FileNotFoundError):
        load_coaches(s, SEASON, tmp_path / "does_not_exist.csv")
