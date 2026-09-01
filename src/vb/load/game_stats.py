"""Load per-game CSV into contests + player_game_stats (the PRIMARY stat fact).

Rows are keyed to players by NCAA PlayerID. Only rows whose player already exists in the
players table (loaded from rosters) are inserted, so player_game_stats.team_id is taken
from the player's roster team — not the (ambiguous) discovering TeamID in the raw frame.
Unresolved rows (opponents not rostered, team summaries with null PlayerID) are skipped.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..log import get_logger
from ..models import Contest, Player, PlayerGameStat
from .common import STAT_COLUMN_MAP, clean_str, ncaa_id_to_team, num, read_csv

log = get_logger(__name__)


def _default_path(season: int) -> Path:
    return settings.exports_dir / f"ncaa_wvb_game_stats_d1_{season}.csv"


def load_game_stats(session: Session, season: int, csv_path: Path | None = None) -> dict:
    path = Path(csv_path) if csv_path else _default_path(season)
    if not path.exists():
        raise FileNotFoundError(f"game-stats CSV not found: {path}")
    df = read_csv(path)

    # Players resolvable by NCAA id for this season -> (player_id, team_id).
    players = {
        pid: (p_id, team_id)
        for pid, p_id, team_id in session.execute(
            select(Player.ncaa_player_id, Player.id, Player.team_id).where(
                Player.season == season, Player.ncaa_player_id.is_not(None)
            )
        ).all()
    }
    # NCAA team id -> local team id, for resolving each contest's home/away teams.
    ncaa_team = {nid: t.id for nid, t in ncaa_id_to_team(session, season).items()}

    contests_seen: set[str] = set()
    stats = skipped = 0
    for _, r in df.iterrows():
        contest_id = clean_str(r.get("ContestID"))
        if not contest_id:
            skipped += 1
            continue
        if contest_id not in contests_seen:
            contest = session.get(Contest, contest_id)
            if contest is None:
                contest = Contest(contest_id=contest_id, season=season)
                session.add(contest)
            # Date is already stored ISO by the scraper; home/away resolve via NCAA ids.
            # `.get()` keeps older CSVs (without these columns) loadable.
            date = clean_str(r.get("Date"))
            if date:
                contest.date = date
            home = ncaa_team.get(clean_str(r.get("HomeTeamNcaaId")))
            away = ncaa_team.get(clean_str(r.get("AwayTeamNcaaId")))
            if home:
                contest.home_team_id = home
            if away:
                contest.away_team_id = away
            # Match result (linescore). `.get()` keeps pre-results CSVs loadable.
            home_sets = num(r.get("HomeSetsWon"))
            away_sets = num(r.get("AwaySetsWon"))
            if home_sets is not None:
                contest.home_sets_won = int(home_sets)
            if away_sets is not None:
                contest.away_sets_won = int(away_sets)
            set_scores = clean_str(r.get("SetScores"))
            if set_scores:
                contest.set_scores = json.loads(set_scores)
            session.flush()
            contests_seen.add(contest_id)

        pid = clean_str(r.get("PlayerID"))
        resolved = players.get(pid) if pid else None
        if resolved is None:
            skipped += 1
            continue
        player_id, team_id = resolved

        pgs = session.get(PlayerGameStat, (contest_id, player_id))
        if pgs is None:
            pgs = PlayerGameStat(
                contest_id=contest_id, player_id=player_id, team_id=team_id, season=season
            )
            session.add(pgs)
        else:
            pgs.team_id = team_id
            pgs.season = season
        for header, attr in STAT_COLUMN_MAP.items():
            if header in df.columns:
                setattr(pgs, attr, num(r.get(header)))
        stats += 1

    session.flush()
    log.info("load_game_stats: %d contests, %d game-stat rows, %d skipped (season %d)",
             len(contests_seen), stats, skipped, season)
    return {"contests": len(contests_seen), "game_stats": stats, "skipped": skipped}
