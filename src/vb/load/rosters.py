"""Load roster CSV rows into the players table (upsert by ncaa_player_id + season)."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..log import get_logger
from ..models import Player
from ..util import height_to_inches, normalize_class
from .common import clean_str, ncaa_id_to_team, num_int, read_csv

log = get_logger(__name__)


def _default_path(season: int) -> Path:
    return settings.exports_dir / f"ncaa_wvb_rosters_d1_{season}.csv"


def load_rosters(session: Session, season: int, csv_path: Path | None = None) -> dict:
    path = Path(csv_path) if csv_path else _default_path(season)
    if not path.exists():
        raise FileNotFoundError(f"roster CSV not found: {path}")
    df = read_csv(path)
    team_map = ncaa_id_to_team(session, season)

    loaded = skipped = 0
    for _, r in df.iterrows():
        ncaa_team_id = clean_str(r.get("TeamID"))
        team = team_map.get(ncaa_team_id) if ncaa_team_id else None
        if team is None:
            skipped += 1
            continue
        pid = clean_str(r.get("PlayerID"))
        name = clean_str(r.get("Player"))
        if not name:
            skipped += 1
            continue

        player = None
        if pid:
            player = session.scalar(
                select(Player).where(Player.ncaa_player_id == pid, Player.season == season)
            )
        if player is None:
            # Fallback identity: (team, name, season) when NCAA id is missing.
            player = session.scalar(
                select(Player).where(
                    Player.team_id == team.id,
                    Player.name == name,
                    Player.season == season,
                    Player.ncaa_player_id.is_(None),
                )
            ) if not pid else None
        if player is None:
            player = Player(season=season, name=name, ncaa_player_id=pid, team_id=team.id)
            session.add(player)

        player.team_id = team.id
        player.name = name
        player.number = num_int(r.get("Number"))
        player.position = clean_str(r.get("Pos"))
        player.class_year = normalize_class(clean_str(r.get("Yr")) or "") or None
        player.height_inches = height_to_inches(r.get("Ht"))
        player.hometown = clean_str(r.get("Hometown"))
        player.high_school = clean_str(r.get("High School"))
        loaded += 1

    session.flush()
    log.info("load_rosters: %d players upserted, %d rows skipped (season %d)",
             loaded, skipped, season)
    return {"players": loaded, "skipped": skipped}
