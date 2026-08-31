"""Season-to-date cumulative stats scraper — VALIDATION ONLY.

The app's cumulative numbers are derived from per-game stats (see vb.derive.cumulative).
This scraper pulls NCAA's *published* season totals so vb.derive.reconcile can compare the
two and flag discrepancies (missing contests, GS gap, etc.). Writes a resumable raw CSV.
"""
from __future__ import annotations

from collections.abc import Iterable
from io import StringIO
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from ..config import settings
from ..fetch import fetch_html
from ..log import get_logger
from .teams_json import season_team_ids

log = get_logger(__name__)

_DROP_LABELS = {"TEAM", "Totals", "Opponent Totals"}


def _inject_player_ids(df: pd.DataFrame, html: str) -> pd.DataFrame:
    if "Player" not in df.columns:
        return df
    soup = BeautifulSoup(html, "html.parser")
    id_map: dict[str, str] = {}
    for link in soup.select("table a[href^='/players/']"):
        pid = link.get("href", "").rstrip("/").split("/")[-1]
        name = link.get_text(strip=True)
        if pid and name:
            id_map[name] = pid
    if not id_map:
        return df
    df = df.copy()
    df.insert(df.columns.get_loc("Player"), "PlayerID", df["Player"].map(id_map))
    return df


def _extract_player_table(html: str) -> pd.DataFrame | None:
    for t in pd.read_html(StringIO(html)):
        if "Player" in t.columns and not t.empty:
            if "PlayerID" not in t.columns:
                t = _inject_player_ids(t, html)
            return t
    return None


def scrape_season_stats(
    team_ids: Iterable[str],
    year: int,
    output: Path | None = None,
) -> Path:
    out = Path(output) if output else (
        settings.exports_dir / f"ncaa_wvb_player_stats_d1_{year}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = season_team_ids(year)
    done: set[str] = set()
    if out.exists():
        try:
            done = set(pd.read_csv(out, usecols=["TeamID"], dtype={"TeamID": str})["TeamID"])
            log.info("[resume] %d team(s) already in season-stats CSV; skipping.", len(done))
        except Exception:
            pass

    team_ids = [str(t) for t in team_ids]
    for i, tid in enumerate(team_ids, 1):
        if tid in done:
            continue
        entry = meta.get(tid, {})
        team_name = entry.get("team") or entry.get("short_name") or ""
        conference = entry.get("conference", "")
        url = f"https://stats.ncaa.org/teams/{tid}/season_to_date_stats"
        html = fetch_html(url, wait_selectors=('table:has-text("Player")', "table"))
        table = _extract_player_table(html)
        if table is None or table.empty:
            log.info("[season %d/%d] team_id=%s: no stats", i, len(team_ids), tid)
            continue
        if "#" in table.columns:
            table = table.rename(columns={"#": "Number"})
        if "Player" in table.columns:
            table = table[~table["Player"].isin(_DROP_LABELS)]
        table.insert(0, "Season", f"{year}-{year + 1}")
        table.insert(1, "TeamID", tid)
        table.insert(2, "Team", team_name)
        table.insert(3, "Conference", conference)
        table.to_csv(out, mode="a", header=not out.exists(), index=False)
        log.info("[season %d/%d] team_id=%s players=%d", i, len(team_ids), tid, len(table))
    return out
