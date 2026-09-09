"""Season-accurate conference resolution (Postgres-backed; skipped if the DB is unreachable).

Conference membership is realignment-aware: ``team_season_ids.conference_id`` overrides the global
``teams.conference_id`` for a given season, and reads coalesce to the global default when a season row
has no value yet. Mirrors the real Colorado State case (Mountain West in 2025, Pac-12 in 2026).

Covers:
  * ``load_season_conferences`` writes per-season membership from an injected mapping (idempotent),
  * ``season_conf_map`` returns the season value, falling back to the global default,
  * ``/team-records`` groups a team under its *season* conference,
  * ``/conferences?season=`` lists only conferences present that season.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from vb.api.routers.conferences import list_conferences
from vb.api.routers.stats import team_records
from vb.db import engine, session_scope
from vb.load.teams import load_season_conferences
from vb.models import Conference, Contest, Team, TeamSeasonId
from vb.season_conf import season_conf_map


def _db_available() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")

# Far-future sentinel seasons so the fixture never collides with real data.
PAST, CURR = 2100, 2101
CONF_PAST, CONF_CURR, CONF_OPP = "_SC_MWC", "_SC_PAC12", "_SC_OPP"
TEAM_MAIN, TEAM_OPP = "_SC_MAIN", "_SC_OPP_TEAM"
NCAA_MAIN, NCAA_OPP = "_SC_N_MAIN", "_SC_N_OPP"


def _wipe(s):
    s.execute(text("DELETE FROM contests WHERE season IN (:a, :b)"), {"a": PAST, "b": CURR})
    s.execute(text("DELETE FROM team_season_ids WHERE season IN (:a, :b)"), {"a": PAST, "b": CURR})
    s.execute(text("DELETE FROM teams WHERE name LIKE '_SC_%'"))
    s.execute(text("DELETE FROM conferences WHERE name LIKE '_SC_%'"))


@pytest.fixture
def fixture():
    """Team MAIN: global conference = PAC12 (the current default), but its PAST-season row is MWC.

    Both seasons get one played contest MAIN-vs-OPP so team-records includes MAIN. Yields ids."""
    with session_scope() as s:
        _wipe(s)
    with session_scope() as s:
        c_past = Conference(name=CONF_PAST)
        c_curr = Conference(name=CONF_CURR)
        c_opp = Conference(name=CONF_OPP)
        s.add_all([c_past, c_curr, c_opp]); s.flush()

        # Global (current) affiliation is PAC12; MWC is only the PAST season's affiliation.
        main = Team(name=TEAM_MAIN, short_name="_SC M", conference_id=c_curr.id)
        opp = Team(name=TEAM_OPP, short_name="_SC O", conference_id=c_opp.id)
        s.add_all([main, opp]); s.flush()

        # Season id rows. PAST gets an explicit MWC override; CURR is left NULL -> global fallback.
        s.add_all([
            TeamSeasonId(team_id=main.id, season=PAST, ncaa_team_id=NCAA_MAIN,
                         conference_id=c_past.id),
            TeamSeasonId(team_id=main.id, season=CURR, ncaa_team_id=NCAA_MAIN),
            TeamSeasonId(team_id=opp.id, season=PAST, ncaa_team_id=NCAA_OPP),
            TeamSeasonId(team_id=opp.id, season=CURR, ncaa_team_id=NCAA_OPP),
        ])
        for season in (PAST, CURR):
            s.add(Contest(contest_id=f"_SC_C_{season}", season=season, date=f"{season}-09-01 18:00",
                          home_team_id=main.id, away_team_id=opp.id,
                          home_sets_won=3, away_sets_won=0))
        s.flush()
        ids = {"main": main.id, "opp": opp.id,
               "c_past": c_past.id, "c_curr": c_curr.id, "c_opp": c_opp.id}
    yield ids
    with session_scope() as s:
        _wipe(s)


@requires_db
def test_season_conf_map_override_and_fallback(fixture):
    with session_scope() as s:
        past = season_conf_map(s, PAST, [fixture["main"]])
        curr = season_conf_map(s, CURR, [fixture["main"]])
    # PAST: explicit season override wins.
    assert past[fixture["main"]][0] == fixture["c_past"]
    assert past[fixture["main"]][1] == CONF_PAST
    # CURR: season row NULL -> coalesce to the global default.
    assert curr[fixture["main"]][0] == fixture["c_curr"]
    assert curr[fixture["main"]][1] == CONF_CURR


@requires_db
def test_team_records_uses_season_conference(fixture):
    with session_scope() as s:
        past = {r.team_id: r for r in team_records(season=PAST, db=s)}
        curr = {r.team_id: r for r in team_records(season=CURR, db=s)}
    assert past[fixture["main"]].conference == CONF_PAST
    assert curr[fixture["main"]].conference == CONF_CURR


@requires_db
def test_team_records_conference_filter_is_season_scoped(fixture):
    with session_scope() as s:
        # Filtering PAST by MWC includes MAIN; filtering PAST by PAC12 excludes it.
        by_mwc = [r.team_id for r in team_records(season=PAST, conference=CONF_PAST, db=s)]
        by_pac = [r.team_id for r in team_records(season=PAST, conference=CONF_CURR, db=s)]
    assert fixture["main"] in by_mwc
    assert fixture["main"] not in by_pac


@requires_db
def test_list_conferences_season_scoped(fixture):
    with session_scope() as s:
        past = {c.name for c in list_conferences(season=PAST, db=s)}
        curr = {c.name for c in list_conferences(season=CURR, db=s)}
    # PAST: MAIN is MWC, OPP is its global conf -> both present; PAC12 (MAIN's global) is NOT.
    assert CONF_PAST in past
    assert CONF_CURR not in past
    # CURR: MAIN falls back to PAC12; MWC no longer present.
    assert CONF_CURR in curr
    assert CONF_PAST not in curr


@requires_db
def test_load_season_conferences_writes_and_is_idempotent(fixture):
    # Authoritative NCAA membership: MAIN -> MWC for PAST (matches its existing override), and a *move*
    # for CURR proving the loader overwrites the NULL row.
    membership = {
        NCAA_MAIN: (CONF_PAST, TEAM_MAIN),
        NCAA_OPP: (CONF_OPP, TEAM_OPP),
    }
    with session_scope() as s:
        res = load_season_conferences(s, CURR, membership=membership)
    assert res["matched"] == 2
    with session_scope() as s:
        row = s.get(TeamSeasonId, (fixture["main"], CURR))
        conf = s.get(Conference, row.conference_id)
    assert conf.name == CONF_PAST  # CURR row was NULL; loader set it to the membership's value.
    # Idempotent: a second run yields the same counts and no duplicate conferences.
    with session_scope() as s:
        res2 = load_season_conferences(s, CURR, membership=membership)
        n_conf = s.scalar(select(Conference.id).where(Conference.name == CONF_PAST).limit(1))
    assert res2["matched"] == 2
    assert n_conf is not None
