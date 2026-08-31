"""Load the team dimension from teams.json into conferences/teams/team_season_ids/coaches.

Idempotent: upserts by natural keys (conference name, team name, (team,season) id,
(team,name,title,season) coach). Only core identity + location + logos + aliases are kept —
no scorecard/airport/niche fields (out of scope).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..log import get_logger
from ..models import Coach, Conference, Team, TeamSeasonId
from ..scrape.teams_json import load_teams as load_teams_json
from .common import clean_str

log = get_logger(__name__)


def _get_or_create_conference(session: Session, name: str | None) -> Conference | None:
    name = clean_str(name)
    if not name:
        return None
    conf = session.scalar(select(Conference).where(Conference.name == name))
    if conf is None:
        conf = Conference(name=name)
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
    team.notes = clean_str(entry.get("notes"))
    session.flush()
    return team


def _upsert_season_id(session: Session, team: Team, season: int, ncaa_id: str) -> None:
    row = session.get(TeamSeasonId, (team.id, season))
    if row is None:
        session.add(TeamSeasonId(team_id=team.id, season=season, ncaa_team_id=str(ncaa_id)))
    else:
        row.ncaa_team_id = str(ncaa_id)


def _upsert_coaches(session: Session, team: Team, entry: dict, season: int) -> int:
    n = 0
    for i, c in enumerate(entry.get("coaches") or []):
        name = clean_str(c.get("name"))
        if not name:
            continue
        title = clean_str(c.get("title"))
        coach = session.scalar(
            select(Coach).where(
                Coach.team_id == team.id,
                Coach.name == name,
                Coach.title.is_(title) if title is None else Coach.title == title,
                Coach.season == season,
            )
        )
        if coach is None:
            coach = Coach(team_id=team.id, name=name, title=title, season=season)
            session.add(coach)
        coach.email = clean_str(c.get("email"))
        coach.phone = clean_str(c.get("phone"))
        coach.sort_order = i
        n += 1
    return n


def load_teams(session: Session, season: int, path: str | None = None) -> dict:
    """Upsert all teams; season-scoped for team_season_ids + coaches. Returns counts."""
    entries = load_teams_json(path)
    teams = seasons = coaches = 0
    for entry in entries:
        team = _upsert_team(session, entry)
        if team is None:
            continue
        teams += 1
        ncaa_id = (entry.get("ncaa_team_ids") or {}).get(str(season))
        if ncaa_id:
            _upsert_season_id(session, team, season, str(ncaa_id))
            seasons += 1
        coaches += _upsert_coaches(session, team, entry, season)
    session.flush()
    log.info("load_teams: %d teams, %d season ids, %d coaches (season %d)",
             teams, seasons, coaches, season)
    return {"teams": teams, "season_ids": seasons, "coaches": coaches}
