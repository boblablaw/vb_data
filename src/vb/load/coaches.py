"""Load the NCAA-scraped head-coach CSV into the coaches table.

The roster scrape (scrape/rosters.py :: _extract_coach) writes one head-coach row per team to
``ncaa_wvb_coaches_d1_<season>.csv`` (TeamID, Team, CoachName, CoachId, Seasons, Record). This
loader replaces the season's coaches wholesale: it deletes existing rows for the season first (which
evicts the legacy teams.json-sourced assistant rows), then inserts one Head Coach per CSV line.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..config import settings
from ..log import get_logger
from ..models import Coach
from .common import clean_str, ncaa_id_to_team, read_csv

log = get_logger(__name__)


def _default_path(season: int) -> Path:
    return settings.staging_dir / f"ncaa_wvb_coaches_d1_{season}.csv"


def load_coaches(session: Session, season: int, csv_path: Path | None = None) -> dict:
    """Replace the season's coaches with the NCAA head coach for each team. Returns counts."""
    path = Path(csv_path) if csv_path else _default_path(season)
    if not path.exists():
        raise FileNotFoundError(f"coaches CSV not found: {path}")
    df = read_csv(path)
    team_map = ncaa_id_to_team(session, season)

    # Season-scoped replace: drop legacy (teams.json) coach rows before inserting NCAA head coaches.
    session.execute(delete(Coach).where(Coach.season == season))

    loaded = skipped = 0
    for _, r in df.iterrows():
        name = clean_str(r.get("CoachName"))
        team = team_map.get(str(r.get("TeamID")))
        if not name or team is None:
            skipped += 1
            continue
        session.add(Coach(
            team_id=team.id,
            season=season,
            name=name,
            title="Head Coach",
            sort_order=0,
            ncaa_coach_id=clean_str(r.get("CoachId")),
            seasons=clean_str(r.get("Seasons")),
            record=clean_str(r.get("Record")),
        ))
        loaded += 1
    session.flush()
    log.info("load_coaches: %d head coaches, %d skipped (season %d)", loaded, skipped, season)
    return {"coaches": loaded, "skipped": skipped}
