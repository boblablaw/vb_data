"""Load the team dimension from teams.json into conferences/teams/team_season_ids.

Idempotent: upserts by natural keys (conference name, team name, (team,season) id). Only core
identity + location + logos + aliases are kept — no scorecard/airport/niche fields (out of scope).
Coaches are NOT loaded here — head coaches come from the NCAA roster scrape via load/coaches.py.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..log import get_logger
from ..models import Conference, Team, TeamSeasonId
from ..scrape.teams_json import load_teams as load_teams_json
from .common import clean_str

log = get_logger(__name__)

# Akamai fronts stats.ncaa.org and blocks by IP reputation. A burst of requests to the sensitive
# inst_team_list endpoint (the only source for per-season conference membership) can get the box's
# IP flagged. To make that impossible from repeated runs, we persist a cooldown marker on the FIRST
# Access Denied and refuse to touch NCAA again until it expires (overridable with force=True). The
# per-request abort already happens in fetch_html (no retry on Access Denied); this guards the
# invocation level.
_DENY_COOLDOWN_HOURS = 6.0
_DENY_MARKER = "ncaa_access_denied.cooldown"


def _cooldown_path() -> Path:
    return settings.staging_dir / _DENY_MARKER


def _check_cooldown(force: bool) -> None:
    """Abort (without making any request) if a recent Access Denied is still cooling off."""
    if force:
        return
    p = _cooldown_path()
    if not p.exists():
        return
    try:
        elapsed = time.time() - float(p.read_text().strip())
    except (ValueError, OSError):
        return
    remaining = _DENY_COOLDOWN_HOURS * 3600 - elapsed
    if remaining > 0:
        raise SystemExit(
            f"stats.ncaa.org last returned Access Denied {elapsed / 3600:.1f}h ago; cooling off to "
            f"protect the box's IP reputation. Retry in {remaining / 3600:.1f}h, or pass --force."
        )


def _record_denied() -> None:
    try:
        _cooldown_path().write_text(str(time.time()))
    except OSError:
        pass


def _clear_cooldown() -> None:
    try:
        _cooldown_path().unlink(missing_ok=True)
    except OSError:
        pass


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
    team.website = clean_str(entry.get("url"))
    team.stats_url = clean_str(entry.get("stats_url"))
    team.aliases = entry.get("team_name_aliases") or None
    session.flush()
    return team


def _upsert_season_id(session: Session, team: Team, season: int, ncaa_id: str) -> None:
    row = session.get(TeamSeasonId, (team.id, season))
    if row is None:
        session.add(TeamSeasonId(team_id=team.id, season=season, ncaa_team_id=str(ncaa_id)))
    else:
        row.ncaa_team_id = str(ncaa_id)


def load_season_conferences(
    session: Session,
    season: int,
    membership: dict[str, tuple[str, str]] | None = None,
    force: bool = False,
) -> dict:
    """Populate ``team_season_ids.conference_id`` with each team's conference *for this season*.

    Membership is the authoritative NCAA per-season mapping ``{ncaa_team_id -> (conf_name, team)}``
    from :func:`vb.scrape.team_list.fetch_conference_membership` (fetched live when not supplied —
    tests inject it). We match by ``ncaa_team_id`` against the season's ``team_season_ids`` rows, so
    realignment (e.g. Colorado State: Mountain West in 2025, Pac-12 in 2026) is captured correctly.
    Conferences are resolved via :func:`_get_or_create_conference`, so a newly-seen league is created.
    Idempotent; safe to re-run.
    """
    if membership is None:
        _check_cooldown(force)  # bail before any request if a recent denial is still cooling off
        from ..scrape.team_list import fetch_conference_membership
        try:
            membership = fetch_conference_membership(season)
        except RuntimeError as e:
            if "Access Denied" in str(e):
                _record_denied()  # start the cooldown so repeated runs can't burst-flag the box
                raise SystemExit(
                    "stats.ncaa.org returned Access Denied — aborting without retry so we don't "
                    f"flag the box's IP. A {_DENY_COOLDOWN_HOURS:.0f}h cooldown is now in effect "
                    "(re-run after it expires, or pass --force to override)."
                ) from None
            raise
        _clear_cooldown()  # a clean fetch means the IP is fine again — lift any prior cooldown

    rows = session.scalars(
        select(TeamSeasonId).where(TeamSeasonId.season == season)
    ).all()

    conf_id_cache: dict[str, int] = {}
    matched = unmatched = 0
    for row in rows:
        hit = membership.get(str(row.ncaa_team_id))
        if not hit:
            unmatched += 1
            continue
        conf_name = clean_str(hit[0])
        if not conf_name:
            unmatched += 1
            continue
        cid = conf_id_cache.get(conf_name)
        if cid is None:
            conf = _get_or_create_conference(session, conf_name)
            cid = conf.id if conf else None
            if cid is not None:
                conf_id_cache[conf_name] = cid
        row.conference_id = cid
        matched += 1

    session.flush()
    log.info(
        "load_season_conferences: %d matched, %d unmatched (season %d, %d conferences)",
        matched, unmatched, season, len(conf_id_cache),
    )
    return {"matched": matched, "unmatched": unmatched, "conferences": len(conf_id_cache)}


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
