"""Load the master team dimension from data/teams.json and resolve season NCAA ids."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..config import settings
from ..util import normalize_school_key


@lru_cache(maxsize=4)
def load_teams(path: str | None = None) -> list[dict]:
    p = Path(path) if path else settings.teams_json_path
    if not p.exists():
        raise FileNotFoundError(f"teams.json not found at {p}")
    return json.loads(p.read_text())


def team_id_for(entry: dict, season: int) -> str | None:
    """Return the NCAA team id for a team entry in a given (fall) season year."""
    tid = (entry.get("ncaa_team_ids") or {}).get(str(season))
    return str(tid) if tid else None


def season_team_ids(season: int, path: str | None = None) -> dict[str, dict]:
    """Map ncaa_team_id -> team entry for all teams that have an id for the season."""
    out: dict[str, dict] = {}
    for entry in load_teams(path):
        tid = team_id_for(entry, season)
        if tid:
            out[tid] = entry
    return out


def aliases_for(entry: dict) -> list[str]:
    aliases: list[str] = []
    cands = [entry.get("team"), entry.get("short_name")] + (entry.get("team_name_aliases") or [])
    for c in cands:
        norm = normalize_school_key(c or "")
        if norm and norm not in aliases:
            aliases.append(norm)
    return aliases
