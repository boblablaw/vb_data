"""Shared helpers for CSV loaders: numeric coercion, stat-column mapping, team lookups."""
from __future__ import annotations

import math

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Team, TeamSeasonId

# NCAA stat header -> PlayerGameStat / *_scraped model attribute (counting stats).
STAT_COLUMN_MAP = {
    "S": "sets",
    "Kills": "kills",
    "Errors": "errors",
    "Total Attacks": "total_attacks",
    "Assists": "assists",
    "Aces": "aces",
    "SErr": "serr",
    "Digs": "digs",
    "RetAtt": "retatt",
    "RErr": "rerr",
    "Block Solos": "block_solos",
    "Block Assists": "block_assists",
    "BErr": "berr",
    "PTS": "pts",
    "BHE": "bhe",
}


def num(value) -> float | None:
    """Coerce a CSV cell to float, or None for blanks/NaN/non-numeric."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "-", "/"}:
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def num_int(value) -> int | None:
    f = num(value)
    return int(f) if f is not None else None


def clean_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip()
    return s or None


def ncaa_id_to_team(session: Session, season: int) -> dict[str, Team]:
    """Map ncaa_team_id -> Team for a season (via team_season_ids)."""
    rows = session.execute(
        select(TeamSeasonId.ncaa_team_id, Team)
        .join(Team, Team.id == TeamSeasonId.team_id)
        .where(TeamSeasonId.season == season)
    ).all()
    return {str(ncaa_id): team for ncaa_id, team in rows}


def read_csv(path) -> pd.DataFrame:
    """Read a raw scrape CSV keeping ids as strings; empty frame if missing.

    NCAA id columns are forced to ``str`` so pandas never infers a numeric dtype for them.
    This matters for columns that sometimes have blanks: ``HomeTeamNcaaId`` occasionally
    lacks a value, which would make pandas read the whole column as float (``624845.0``),
    and ``clean_str`` would then yield ``"624845.0"`` — never matching the ``"624845"``
    key in ``ncaa_id_to_team``. (dtype keys for columns absent from the file are ignored.)
    """
    return pd.read_csv(
        path,
        dtype={
            "TeamID": str, "PlayerID": str, "ContestID": str,
            "AwayTeamNcaaId": str, "HomeTeamNcaaId": str,
        },
        keep_default_na=True,
    )
