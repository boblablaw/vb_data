"""ncaa.com game-id mapping (Postgres-backed, self-seeding).

Our ``contest_id`` is a stats.ncaa.org id, which does NOT match ncaa.com/game/<id>. ``map_ncaa_games``
pulls ncaa.com's own scoreboard and matches each ncaa.com game to our contests/schedule rows on
(date + unordered team pair), then writes the recovered ncaa.com id. These tests stub the network
(``fetch_games``) and assert the id lands on both the played contest and both schedule perspectives,
that non-matching pairs are left untouched, and that the ``/games`` payload surfaces the id.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from vb.db import engine, session_scope
from vb.load import map_ncaa_games
from vb.models import Conference, Contest, Schedule, Team
from vb.scrape.ncaa_com_games import NcaaComGame


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

SEASON = 2104  # far-future sentinel, distinct from other test files


def _wipe(s):
    s.execute(text("DELETE FROM schedule WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM contests WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM teams WHERE name LIKE '_MAP_TEAM%'"))
    s.execute(text("DELETE FROM conferences WHERE name LIKE '_MAP_CONF%'"))


@pytest.fixture
def seed():
    with session_scope() as s:
        _wipe(s)
    with session_scope() as s:
        conf = Conference(name="_MAP_CONF"); s.add(conf); s.flush()
        # short_name slugs to the ncaa.com seoname we feed the stub. Deliberately-nonsense schools
        # so they can't collide with a real team in a shared dev DB (which would win the slug map).
        a = Team(name="_MAP_TEAM_A", short_name="Zqmapa Tech", conference_id=conf.id)
        b = Team(name="_MAP_TEAM_B", short_name="Zqmapb St.", conference_id=conf.id)
        c = Team(name="_MAP_TEAM_C", short_name="Zqmapc", conference_id=conf.id)
        s.add_all([a, b, c]); s.flush()
        # Played contest A vs B on 09-01.
        s.add(Contest(
            contest_id="7200001", season=SEASON, date="2104-09-01 19:00",
            home_team_id=a.id, away_team_id=b.id, home_sets_won=3, away_sets_won=1,
        ))
        # Upcoming A vs B on 09-08, both per-team perspectives.
        s.add_all([
            Schedule(season=SEASON, team_id=a.id, opponent_team_id=b.id,
                     opponent_name="_MAP_TEAM_B", date="2104-09-08", game_time="07:00 PM",
                     site="home"),
            Schedule(season=SEASON, team_id=b.id, opponent_team_id=a.id,
                     opponent_name="_MAP_TEAM_A", date="2104-09-08", game_time="07:00 PM",
                     site="away"),
        ])
        out = {"a": a.id, "b": b.id, "c": c.id}
    yield out
    with session_scope() as s:
        _wipe(s)


def _stub_fetch(monkeypatch, by_date):
    """Patch load.ncaa_com_games.fetch_games to return canned games keyed by ISO date."""
    def fake(day, season):
        return by_date.get(day.isoformat(), [])

    monkeypatch.setattr("vb.load.ncaa_com_games.fetch_games", fake)


@requires_db
def test_map_populates_contest_and_both_schedule_rows(monkeypatch, seed):
    _stub_fetch(monkeypatch, {
        "2104-09-01": [NcaaComGame(
            ncaa_game_id="6300001", date="2104-09-01",
            seonames=("zqmapa-tech", "zqmapb-st"),
            name_shorts=("Zqmapa Tech", "Zqmapb St."),
            start_epoch=None, game_state="F",
        )],
        "2104-09-08": [NcaaComGame(
            ncaa_game_id="6300002", date="2104-09-08",
            # ncaa.com's isHome can be flipped vs ours -> order reversed here on purpose.
            seonames=("zqmapb-st", "zqmapa-tech"),
            name_shorts=("Zqmapb St.", "Zqmapa Tech"),
            start_epoch=None, game_state="P",
        )],
    })
    with session_scope() as s:
        res = map_ncaa_games(s, SEASON)
    assert res["matched"] == 2

    with session_scope() as s:
        c = s.get(Contest, "7200001")
        assert c.ncaa_game_id == "6300001"
        sched = s.query(Schedule).filter(Schedule.season == SEASON).all()
        assert {r.ncaa_game_id for r in sched} == {"6300002"}  # both perspectives written


@requires_db
def test_map_is_idempotent(monkeypatch, seed):
    _stub_fetch(monkeypatch, {
        "2104-09-01": [NcaaComGame(
            ncaa_game_id="6300001", date="2104-09-01",
            seonames=("zqmapa-tech", "zqmapb-st"),
            name_shorts=("Zqmapa Tech", "Zqmapb St."),
            start_epoch=None, game_state="F",
        )],
    })
    with session_scope() as s:
        first = map_ncaa_games(s, SEASON)
    with session_scope() as s:
        second = map_ncaa_games(s, SEASON)
    assert first["updated"] >= 1
    assert second["updated"] == 0  # nothing changes on the second pass


@requires_db
def test_unmatched_pair_leaves_id_null(monkeypatch, seed):
    """A ncaa.com game whose team pair we can't resolve to two teams is counted unresolved and
    writes nothing."""
    _stub_fetch(monkeypatch, {
        "2104-09-01": [NcaaComGame(
            ncaa_game_id="6300099", date="2104-09-01",
            seonames=("nowhere-state", "elsewhere-tech"),
            name_shorts=("Nowhere St.", "Elsewhere Tech"),
            start_epoch=None, game_state="F",
        )],
    })
    with session_scope() as s:
        res = map_ncaa_games(s, SEASON)
    assert res["unresolved"] >= 1 and res["matched"] == 0
    with session_scope() as s:
        assert s.get(Contest, "7200001").ncaa_game_id is None


@requires_db
def test_games_payload_carries_ncaa_game_id(client, monkeypatch, seed):
    _stub_fetch(monkeypatch, {})  # not used; we set the id directly below
    with session_scope() as s:
        s.get(Contest, "7200001").ncaa_game_id = "6300001"

    game = client.get("/games", params={"season": SEASON, "date": "2104-09-01"}).json()[0]
    assert game["ncaa_game_id"] == "6300001"
