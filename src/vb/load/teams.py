"""Load the team dimension from teams.json into conferences/teams/team_season_ids.

Idempotent: upserts by natural keys (conference name, team name, (team,season) id). Only core
identity + location + logos + aliases are kept — no scorecard/airport/niche fields (out of scope).
Coaches are NOT loaded here — head coaches come from the NCAA roster scrape via load/coaches.py.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..log import get_logger
from ..models import Conference, Team, TeamSeasonId
from ..scrape.teams_json import load_teams as load_teams_json
from .common import clean_str

log = get_logger(__name__)


def _default_conf_short(name: str) -> str:
    """Default short label for a newly-seen conference: the name with a trailing " Conference"
    dropped ("Pac-12 Conference" -> "Pac-12"; league names like "Ivy League" are unchanged).

    Only used when creating a conference row so ``short_name`` is never null. Curated acronyms
    (SEC, MAC, …) are seeded by migration and editable in the DB; existing rows are left untouched.
    """
    return re.sub(r"\s+Conference$", "", name)


def _get_or_create_conference(session: Session, name: str | None) -> Conference | None:
    name = clean_str(name)
    if not name:
        return None
    conf = session.scalar(select(Conference).where(Conference.name == name))
    if conf is None:
        conf = Conference(name=name, short_name=_default_conf_short(name))
        session.add(conf)
        session.flush()
    return conf


def _upsert_team(session: Session, entry: dict) -> Team | None:
    name = clean_str(entry.get("team")) or clean_str(entry.get("short_name"))
    if not name:
        return None
    team = session.scalar(select(Team).where(Team.name == name))
    if team is None:
        team = Team(name=name)
        session.add(team)
    conf = _get_or_create_conference(session, entry.get("conference"))
    team.short_name = clean_str(entry.get("short_name"))
    team.conference_id = conf.id if conf else None
    team.city = clean_str(entry.get("city"))
    team.state = clean_str(entry.get("state"))
    team.latitude = entry.get("lat")
    team.longitude = entry.get("lon")
    team.logo_light = clean_str(entry.get("ncaa_logo_light"))
    team.logo_dark = clean_str(entry.get("ncaa_logo_dark"))
    team.aliases = entry.get("team_name_aliases") or None
    session.flush()
    return team


def _upsert_season_id(session: Session, team: Team, season: int, ncaa_id: str) -> None:
    row = session.get(TeamSeasonId, (team.id, season))
    if row is None:
        session.add(TeamSeasonId(team_id=team.id, season=season, ncaa_team_id=str(ncaa_id)))
    else:
        row.ncaa_team_id = str(ncaa_id)


def load_teams(session: Session, season: int, path: str | None = None) -> dict:
    """Upsert all teams; season-scoped for team_season_ids. Returns counts."""
    entries = load_teams_json(path)
    teams = seasons = 0
    for entry in entries:
        team = _upsert_team(session, entry)
        if team is None:
            continue
        teams += 1
        ncaa_id = (entry.get("ncaa_team_ids") or {}).get(str(season))
        if ncaa_id:
            _upsert_season_id(session, team, season, str(ncaa_id))
            seasons += 1
    session.flush()
    log.info("load_teams: %d teams, %d season ids (season %d)", teams, seasons, season)
    return {"teams": teams, "season_ids": seasons}
