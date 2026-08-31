"""Per-game (contest) per-player stats from stats.ncaa.org — the PRIMARY stat source.

Two steps:
  1. discover_contests(team_id): read a team page for its /contests/<id>/box_score links.
  2. fetch_contest_individual_stats(contest_id): read /contests/<id>/individual_stats,
     which carries a per-player stat table for each of the two teams.

Writes a raw CSV (exports/ncaa_wvb_game_stats_d1_<year>.csv). The scrape is resumable:
contests already present in the CSV are skipped.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from io import StringIO
from pathlib import Path

import pandas as pd

from ..config import settings
from ..fetch import fetch_html
from ..log import get_logger

log = get_logger(__name__)

CONTEST_RE = re.compile(r"/contests/(\d+)/box_score")
PLAYER_LINK_RE = re.compile(r'/players/(\d+)"[^>]*>\s*([^<]+?)\s*<')


def discover_contests(team_id: str) -> list[str]:
    """Unique contest ids (page order) for a team's season."""
    html = fetch_html(f"https://stats.ncaa.org/teams/{team_id}", wait_selectors=("table",))
    out: list[str] = []
    for cid in CONTEST_RE.findall(html):
        if cid not in out:
            out.append(cid)
    return out


def _player_id_map(html: str) -> dict[str, str]:
    return {name.strip(): pid for pid, name in PLAYER_LINK_RE.findall(html)}


def fetch_contest_individual_stats(contest_id: str) -> pd.DataFrame:
    """Per-player stat rows for both teams in a contest (empty if none)."""
    url = f"https://stats.ncaa.org/contests/{contest_id}/individual_stats"
    html = fetch_html(url, wait_selectors=("table",))
    id_map = _player_id_map(html)
    frames: list[pd.DataFrame] = []
    for side, table in enumerate(pd.read_html(StringIO(html))):
        cols = [str(c) for c in table.columns]
        if "Name" not in cols or "Kills" not in cols:
            continue
        t = table.copy()
        t = t[t["Name"].notna()]
        t.insert(0, "ContestID", str(contest_id))
        t.insert(1, "TeamSide", side)
        t.insert(2, "PlayerID", t["Name"].astype(str).str.strip().map(id_map))
        frames.append(t)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    # NCAA's individual_stats table renders each player row twice; collapse identical
    # duplicates so one contest yields one stat line per player.
    out = out.drop_duplicates(subset=["ContestID", "PlayerID", "Name"], keep="first")
    return out.reset_index(drop=True)


def _output_path(year: int, output: Path | None) -> Path:
    if output:
        return Path(output)
    return settings.exports_dir / f"ncaa_wvb_game_stats_d1_{year}.csv"


def _existing_contest_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        df = pd.read_csv(path, usecols=["ContestID"], dtype={"ContestID": str})
        return set(df["ContestID"].astype(str))
    except Exception:
        return set()


def scrape_game_stats(
    team_ids: Iterable[str],
    year: int,
    max_contests: int | None = None,
    output: Path | None = None,
) -> Path:
    """Scrape per-game stats for the given team ids, appending to a resumable CSV."""
    out = _output_path(year, output)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = _existing_contest_ids(out)
    if seen:
        log.info("[resume] %d contest(s) already in %s; skipping.", len(seen), out.name)

    team_ids = [str(t) for t in team_ids]
    for ti, tid in enumerate(team_ids, 1):
        contests = discover_contests(tid)
        if max_contests:
            contests = contests[:max_contests]
        todo = [c for c in contests if c not in seen]
        log.info("[team %d/%d] team_id=%s contests=%d (todo=%d)",
                 ti, len(team_ids), tid, len(contests), len(todo))
        for ci, cid in enumerate(todo, 1):
            df = fetch_contest_individual_stats(cid)
            if df.empty:
                log.info("    [%d/%d] contest %s: no stats", ci, len(todo), cid)
                seen.add(cid)
                continue
            df.insert(0, "TeamID", str(tid))
            df.insert(1, "Season", year)
            df.to_csv(out, mode="a", header=not out.exists(), index=False)
            seen.add(cid)
            log.info("    [%d/%d] contest %s: %d rows", ci, len(todo), cid, len(df))
    return out
