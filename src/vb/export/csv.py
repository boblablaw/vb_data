"""Named DB -> CSV exports. Each export is a SQL query run against Postgres and dumped.

Trivial now that the DB is the source of truth: no merge/pivot code, just SELECTs.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..log import get_logger

log = get_logger(__name__)

# Columns that are conceptually whole numbers but arrive as float64 when the column has any
# NULL (pandas has no native nullable int). Cast to nullable Int64 so they render as e.g. 12,
# not 12.0, while NULLs stay blank.
INT_COLUMNS = {"number"}

# name -> SQL. `:season` is bound when provided.
EXPORTS: dict[str, str] = {
    # Player + team + derived cumulative season stats — the flagship "merged" export.
    "merged": """
        SELECT
            t.name AS team, t.short_name, c.name AS conference, t.state,
            p.ncaa_player_id, p.name AS player, p.number, p.position,
            p.class_year, p.height_inches, p.hometown, p.high_school,
            s.gp, ss.gs, s.sp, s.kills, s.errors, s.total_attacks, s.hit_pct,
            s.assists, s.aces, s.serr, s.digs, s.retatt, s.rerr,
            s.block_solos, s.block_assists, s.total_blocks, s.berr, s.pts, s.bhe,
            s.kills_per_set, s.assists_per_set, s.aces_per_set, s.digs_per_set,
            s.blocks_per_set, s.pts_per_set
        FROM players p
        JOIN teams t ON t.id = p.team_id
        LEFT JOIN conferences c ON c.id = t.conference_id
        LEFT JOIN player_season_stats s
               ON s.player_id = p.id AND s.season = p.season
        LEFT JOIN player_season_stats_scraped ss
               ON ss.player_id = p.id AND ss.season = p.season
        WHERE p.season = :season
        ORDER BY t.name, p.name
    """,
    "rosters": """
        SELECT t.name AS team, c.name AS conference, p.number, p.ncaa_player_id,
               p.name AS player, p.class_year, p.position, p.height_inches,
               p.hometown, p.high_school
        FROM players p
        JOIN teams t ON t.id = p.team_id
        LEFT JOIN conferences c ON c.id = t.conference_id
        WHERE p.season = :season
        ORDER BY t.name, p.number
    """,
    # Per-game stats, one readable row per player-contest: team, opponent, when, and
    # home/away — no raw ids. Opponent/home-away come from contests (populated by the
    # game-stats loader / `vb backfill-contest-meta`).
    "game_stats": """
        SELECT
            t.name AS team,
            opp.name AS opponent,
            con.date AS game_datetime,
            CASE WHEN con.home_team_id = g.team_id THEN 'Home'
                 WHEN con.away_team_id = g.team_id THEN 'Away' END AS side,
            p.number, p.name AS player,
            g.sets, g.kills, g.errors, g.total_attacks, g.assists, g.aces,
            g.serr, g.digs, g.retatt, g.rerr, g.block_solos, g.block_assists,
            g.berr, g.pts, g.bhe
        FROM player_game_stats g
        JOIN players p ON p.id = g.player_id
        JOIN teams t ON t.id = g.team_id
        JOIN contests con ON con.contest_id = g.contest_id
        LEFT JOIN teams opp ON opp.id = CASE WHEN con.home_team_id = g.team_id
                                             THEN con.away_team_id ELSE con.home_team_id END
        WHERE g.season = :season
        ORDER BY con.date, t.name, p.name
    """,
    "teams": """
        SELECT t.name, t.short_name, c.name AS conference, t.city, t.state,
               t.latitude, t.longitude, t.rpi_rank, t.rpi_record,
               tsi.ncaa_team_id
        FROM teams t
        LEFT JOIN conferences c ON c.id = t.conference_id
        LEFT JOIN team_season_ids tsi
               ON tsi.team_id = t.id AND tsi.season = :season
        ORDER BY t.name
    """,
}


def export_csv(session: Session, name: str, season: int | None = None,
               output: Path | None = None) -> Path:
    if name not in EXPORTS:
        raise KeyError(f"unknown export '{name}'. Available: {', '.join(sorted(EXPORTS))}")
    sql = EXPORTS[name]
    params = {"season": season} if ":season" in sql else {}
    if ":season" in sql and season is None:
        raise ValueError(f"export '{name}' requires --season")
    df = pd.read_sql_query(text(sql), session.connection(), params=params)
    for col in INT_COLUMNS & set(df.columns):
        df[col] = df[col].astype("Int64")
    out = Path(output) if output else (
        settings.exports_dir / (f"{name}_{season}.csv" if season else f"{name}.csv")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    log.info("export %s: %d rows -> %s", name, len(df), out)
    return out
