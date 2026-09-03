"""Load the schedule CSV into the ``schedule`` table (idempotent upsert).

Resolves the perspective team by its season NCAA id and the opponent by NCAA id first (exact),
falling back to a normalized name/alias match against the teams table. Opponents that don't match
a D1 team (non-D1 exhibitions) are stored with ``opponent_team_id = NULL`` and shown as plain text.
Upsert key is ``(season, team_id, date, opponent_name)`` so re-loading is safe.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..log import get_logger
from ..models import Schedule, Team
from ..util import normalize_school_key
from .common import clean_str, ncaa_id_to_team

log = get_logger(__name__)


def _default_path(season: int) -> Path:
    return settings.staging_dir / f"ncaa_wvb_schedule_d1_{season}.csv"


def _name_to_team_id(session: Session) -> dict[str, int]:
    """Normalized school key -> team_id, built from each team's name / short_name / aliases."""
    out: dict[str, int] = {}
    for t in session.scalars(select(Team)).all():
        cands = [t.name, t.short_name] + (t.aliases or [])
        for c in cands:
            key = normalize_school_key(c) if c else ""
            if key:
                out.setdefault(key, t.id)
    return out


def load_schedule(session: Session, season: int, csv_path: Path | None = None) -> dict:
    path = Path(csv_path) if csv_path else _default_path(season)
    if not path.exists():
        raise FileNotFoundError(f"schedule CSV not found: {path}")
    df = pd.read_csv(
        path,
        dtype={"TeamNcaaId": str, "OpponentNcaaId": str, "ContestId": str},
        keep_default_na=True,
    )

    ncaa_team = {nid: t.id for nid, t in ncaa_id_to_team(session, season).items()}
    by_name = _name_to_team_id(session)

    # Existing rows for the season, keyed by the unique tuple, for in-place upsert.
    existing = {
        (r.team_id, r.date, r.opponent_name): r
        for r in session.scalars(select(Schedule).where(Schedule.season == season)).all()
    }

    inserted = updated = skipped = unresolved_opp = 0
    for _, r in df.iterrows():
        team_id = ncaa_team.get(clean_str(r.get("TeamNcaaId")))
        date = clean_str(r.get("Date"))
        opp_name = clean_str(r.get("OpponentName"))
        if not team_id or not date or not opp_name:
            skipped += 1
            continue

        opp_id = ncaa_team.get(clean_str(r.get("OpponentNcaaId")))
        if opp_id is None:
            opp_id = by_name.get(normalize_school_key(opp_name))
        if opp_id is None:
            unresolved_opp += 1

        key = (team_id, date, opp_name)
        row = existing.get(key)
        if row is None:
            row = Schedule(season=season, team_id=team_id, date=date, opponent_name=opp_name)
            session.add(row)
            existing[key] = row
            inserted += 1
        else:
            updated += 1
        row.opponent_team_id = opp_id
        row.game_time = clean_str(r.get("Time"))
        row.site = clean_str(r.get("Site"))
        row.neutral_location = clean_str(r.get("NeutralLocation"))
        row.result_raw = clean_str(r.get("ResultRaw"))
        # Keep an id we already have if a later scrape of the same row drops it (blank cell).
        row.contest_id = clean_str(r.get("ContestId")) or row.contest_id

    session.flush()
    log.info(
        "load_schedule: %d inserted, %d updated, %d skipped, %d unresolved opponents (season %d)",
        inserted, updated, skipped, unresolved_opp, season,
    )
    return {
        "inserted": inserted, "updated": updated, "skipped": skipped,
        "unresolved_opponents": unresolved_opp,
    }
