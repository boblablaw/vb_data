"""Ranking snapshots + quality-wins (Postgres-backed, self-seeding).

Seeds a far-future sentinel season with two teams and one played contest, then exercises
``snapshot_rankings`` (idempotent per date) and ``compute_quality_wins`` — whose whole point is
that a win only counts when the beaten team was ranked *as of the game date*. Skipped when
Postgres is unreachable; cleans up after itself.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from vb.db import engine, session_scope
from vb.load.enrichment import snapshot_rankings
from vb.models import Conference, Contest, RankingSnapshot, Team
from vb.query.tools import biggest_upsets, compute_quality_wins


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

SEASON = 2104  # far-future sentinel, distinct from other suites
GAME_DATE = "2104-09-08"


def _wipe(s):
    s.execute(text("DELETE FROM ranking_snapshots WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM contests WHERE season = :y"), {"y": SEASON})
    s.execute(text("DELETE FROM teams WHERE name LIKE '_RS_TEAM%'"))
    s.execute(text("DELETE FROM conferences WHERE name LIKE '_RS_CONF%'"))


@pytest.fixture
def seed():
    """Teams A/B in a conference; A beats B 3-1 on GAME_DATE."""
    with session_scope() as s:
        _wipe(s)
    with session_scope() as s:
        conf = Conference(name="_RS_CONF"); s.add(conf); s.flush()
        a = Team(name="_RS_TEAM_A", short_name="RsA", conference_id=conf.id)
        b = Team(name="_RS_TEAM_B", short_name="RsB", conference_id=conf.id)
        s.add_all([a, b]); s.flush()
        s.add(Contest(
            contest_id="8100001", season=SEASON, date=GAME_DATE,
            home_team_id=a.id, away_team_id=b.id, home_sets_won=3, away_sets_won=1,
        ))
        out = {"a": a.id, "b": b.id}
    yield out
    with session_scope() as s:
        _wipe(s)


@requires_db
def test_snapshot_rankings_writes_and_is_idempotent(seed):
    # snapshot_rankings captures *every* currently-ranked team, so we assert on our sentinel team
    # (B) and on total row-count stability across a re-run — not on an absolute snapshotted count.
    with session_scope() as s:
        b = s.get(Team, seed["b"])
        b.avca_rank = 5
        b.rpi_rank = 11
        b.rpi_record = "10-1"
    day = date(2104, 9, 1)
    with session_scope() as s:
        snapshot_rankings(s, SEASON, day)
    with session_scope() as s:
        count1 = s.query(RankingSnapshot).filter(
            RankingSnapshot.season == SEASON, RankingSnapshot.as_of == day
        ).count()
    with session_scope() as s:
        snapshot_rankings(s, SEASON, day)  # re-run same day upserts in place, no new rows
    with session_scope() as s:
        count2 = s.query(RankingSnapshot).filter(
            RankingSnapshot.season == SEASON, RankingSnapshot.as_of == day
        ).count()
        assert count1 == count2  # idempotent: re-run did not add rows
        rows = s.query(RankingSnapshot).filter(
            RankingSnapshot.season == SEASON, RankingSnapshot.as_of == day,
            RankingSnapshot.team_id == seed["b"],
        ).all()
        assert len(rows) == 1
        assert rows[0].avca_rank == 5 and rows[0].rpi_rank == 11


@requires_db
def test_quality_win_counts_when_opponent_ranked_as_of_game_date(seed):
    # B ranked #5 (AVCA) as of BEFORE the game -> A's win over B is a quality win.
    with session_scope() as s:
        s.get(Team, seed["b"]).avca_rank = 5
    with session_scope() as s:
        snapshot_rankings(s, SEASON, date(2104, 9, 1))
    with session_scope() as s:
        res = compute_quality_wins(s, poll="avca", threshold=25, season=SEASON)
        entry = next((e for e in res if e["team_id"] == seed["a"]), None)
        assert entry is not None and entry["quality_wins"] == 1
        win = entry["wins"][0]
        assert win["opponent_id"] == seed["b"] and win["rank_at_time"] == 5
        assert win["date"] == GAME_DATE and win["score"] == "3-1"
        assert win["contest_id"] == "8100001"


@requires_db
def test_quality_win_excluded_when_ranking_came_after_game(seed):
    # B only becomes ranked AFTER the game -> no rank-at-time -> not a quality win.
    with session_scope() as s:
        s.get(Team, seed["b"]).avca_rank = 5
    with session_scope() as s:
        snapshot_rankings(s, SEASON, date(2104, 9, 20))  # after GAME_DATE
    with session_scope() as s:
        res = compute_quality_wins(s, poll="avca", threshold=25, season=SEASON)
        entry = next((e for e in res if e["team_id"] == seed["a"]), None)
        assert entry is None


@requires_db
def test_biggest_upsets_rpi_gap(seed):
    # A (RPI #50) beats B (RPI #5) -> a 45-spot RPI upset, as of the game date.
    with session_scope() as s:
        a, b = s.get(Team, seed["a"]), s.get(Team, seed["b"])
        a.rpi_rank, a.rpi_record = 50, "1-1"
        b.rpi_rank, b.rpi_record = 5, "2-0"
    with session_scope() as s:
        snapshot_rankings(s, SEASON, date(2104, 9, 1))  # before GAME_DATE
    with session_scope() as s:
        ups = biggest_upsets(s, poll="rpi", season=SEASON, limit=100)
        u = next((x for x in ups if x["contest_id"] == "8100001"), None)
        assert u is not None
        assert u["winner_id"] == seed["a"] and u["loser_id"] == seed["b"]
        assert u["winner_rpi"] == 50 and u["loser_rpi"] == 5 and u["gap"] == 45
        assert u["score"] == "3-1" and u["date"] == GAME_DATE


@requires_db
def test_biggest_upsets_avca_over_ranked_team(seed):
    # Unranked (AVCA) A beats AVCA #3 B -> an AVCA upset even without a winner rank.
    with session_scope() as s:
        b = s.get(Team, seed["b"])
        b.avca_rank, b.rpi_rank = 3, 5
    with session_scope() as s:
        snapshot_rankings(s, SEASON, date(2104, 9, 1))
    with session_scope() as s:
        ups = biggest_upsets(s, poll="avca", season=SEASON, limit=100)
        u = next((x for x in ups if x["contest_id"] == "8100001"), None)
        assert u is not None
        assert u["winner_id"] == seed["a"] and u["winner_avca"] is None
        assert u["loser_id"] == seed["b"] and u["loser_avca"] == 3


@requires_db
def test_biggest_upsets_excludes_non_upset(seed):
    # A (RPI #5) beats B (RPI #50): favorite won, so it must NOT appear as an upset.
    with session_scope() as s:
        a, b = s.get(Team, seed["a"]), s.get(Team, seed["b"])
        a.rpi_rank, b.rpi_rank = 5, 50
    with session_scope() as s:
        snapshot_rankings(s, SEASON, date(2104, 9, 1))
    with session_scope() as s:
        ups = biggest_upsets(s, poll="rpi", season=SEASON, limit=100)
        assert not any(x["contest_id"] == "8100001" for x in ups)
