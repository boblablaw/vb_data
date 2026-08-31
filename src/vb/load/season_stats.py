"""Load season-to-date scraped totals into player_season_stats_scraped (VALIDATION ONLY).

These are NCAA's published cumulative numbers. vb.derive.reconcile compares them against the
derived matview to flag gaps and to backfill GS (which per-game data lacks).
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..log import get_logger
from ..models import Player, PlayerSeasonStatScraped
from .common import STAT_COLUMN_MAP, clean_str, num, read_csv

log = get_logger(__name__)

# Season CSV has extra header cells beyond the shared counting stats.
_EXTRA = {"GP": "gp", "GS": "gs", "Hit Pct": "hit_pct", "Trpl Dbl": "trpl_dbl"}


def _default_path(season: int) -> Path:
    return settings.exports_dir / f"ncaa_wvb_player_stats_d1_{season}.csv"


def load_season_stats(session: Session, season: int, csv_path: Path | None = None) -> dict:
    path = Path(csv_path) if csv_path else _default_path(season)
    if not path.exists():
        raise FileNotFoundError(f"season-stats CSV not found: {path}")
    df = read_csv(path)
    players = {
        pid: p_id
        for pid, p_id in session.execute(
            select(Player.ncaa_player_id, Player.id).where(
                Player.season == season, Player.ncaa_player_id.is_not(None)
            )
        ).all()
    }

    loaded = skipped = 0
    for _, r in df.iterrows():
        pid = clean_str(r.get("PlayerID"))
        player_id = players.get(pid) if pid else None
        if player_id is None:
            skipped += 1
            continue
        row = session.get(PlayerSeasonStatScraped, (player_id, season))
        if row is None:
            row = PlayerSeasonStatScraped(player_id=player_id, season=season)
            session.add(row)
        # "S" in the season table means sets played -> sp (not a per-contest sets count).
        for header, attr in STAT_COLUMN_MAP.items():
            if header == "S":
                continue
            if header in df.columns:
                setattr(row, attr, num(r.get(header)))
        if "S" in df.columns:
            row.sp = num(r.get("S"))
        for header, attr in _EXTRA.items():
            if header in df.columns:
                setattr(row, attr, num(r.get(header)))
        loaded += 1

    session.flush()
    log.info("load_season_stats: %d rows upserted, %d skipped (season %d)",
             loaded, skipped, season)
    return {"season_stats": loaded, "skipped": skipped}
