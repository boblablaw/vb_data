"""Roster + coach scraping from stats.ncaa.org team roster pages.

Ported from the original vb_scraper ``ncaa_wvb_scraper.py`` parsers. Writes resumable raw
CSVs: rosters and coaches. One page load per team.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from ..config import settings
from ..fetch import fetch_html
from ..log import get_logger
from .teams_json import season_team_ids

log = get_logger(__name__)

ROSTER_COLS = [
    "Season", "TeamID", "Team", "Conference", "Number", "PlayerID",
    "Player", "Yr", "Pos", "Ht", "Hometown", "High School",
]


def _extract_roster_table(html: str) -> pd.DataFrame | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates = soup.find_all(
        "table", id=lambda x: x and x.startswith("rosters_form_players_")
    )
    table = None
    for t in candidates:
        tbody = t.find("tbody")
        if tbody and tbody.find("tr"):
            table = t
            break
    if table is None:
        table = soup.find("table", id="stat_grid") or soup.find("table")
    if not table:
        return None

    tbody = table.find("tbody") or table
    header_cells = table.find_all("th")
    headers = [th.get_text(strip=True) for th in header_cells] if header_cells else []
    name_idx = headers.index("Name") if "Name" in headers else None

    rows_data = []
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        row = []
        for idx, td in enumerate(cells):
            text = td.get_text(strip=True)
            if name_idx is not None and idx == name_idx:
                link = td.find("a")
                pid = None
                if link and link.get("href", "").startswith("/players/"):
                    pid = link.get("href", "").rstrip("/").split("/")[-1]
                row.append(pid)
                row.append(text)
            else:
                row.append(text)
        rows_data.append(row)
    if not rows_data:
        return None

    if not headers:
        headers = [f"col{i}" for i in range(len(rows_data[0]))]
    col_builder: list[str] = []
    for idx, h in enumerate(headers):
        if name_idx is not None and idx == name_idx:
            col_builder.append("PlayerID")
            col_builder.append("Player")
        else:
            col_builder.append(h)
    df = pd.DataFrame(rows_data, columns=col_builder)

    rename_map = {
        "#": "Number", "Name": "Player", "Class": "Yr", "Position": "Pos",
        "Height": "Ht", "Hometown": "Hometown", "High School": "High School",
    }
    for col in list(df.columns):
        lc = col.lower()
        if lc == "player":
            rename_map[col] = "Player"
        if lc == "playerid":
            rename_map[col] = "PlayerID"
    df = df.rename(columns=rename_map)
    if "Number" in df.columns:
        df["Number"] = pd.to_numeric(df["Number"], errors="coerce")
    keep = [c for c in ["Number", "PlayerID", "Player", "Yr", "Pos", "Ht",
                        "Hometown", "High School"] if c in df.columns]
    return df[keep]


def _extract_coach(html: str, team_id: str, team_name: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    coach_card = soup.find(
        "div", class_="card-header",
        string=lambda s: s and s.strip().lower() == "coach",
    )
    if not coach_card:
        return None
    parent = coach_card.find_parent("div", class_="card")
    body = parent.find("div", class_="card-body") if parent else None
    if not body:
        return None
    name = coach_id = seasons = record = ""
    dl = body.find("dl")
    if dl:
        pairs = list(dl.find_all(["dt", "dd"]))
        for i in range(0, len(pairs), 2):
            dt = pairs[i]
            dd = pairs[i + 1] if i + 1 < len(pairs) else None
            label = dt.get_text(strip=True).lower() if dt else ""
            value = dd.get_text(strip=True) if dd else ""
            if label == "name:":
                name = value
                link = dd.find("a") if dd else None
                if link and link.get("href", "").startswith("/people/"):
                    coach_id = link.get("href", "").rstrip("/").split("/")[-1].split("?", 1)[0]
            elif label == "seasons:":
                seasons = value
            elif label == "record:":
                record = value
    if not name:
        return None
    return {
        "TeamID": str(team_id), "Team": team_name, "CoachName": name,
        "CoachId": coach_id, "Seasons": seasons, "Record": record,
    }


def scrape_rosters(
    team_ids: Iterable[str],
    year: int,
    roster_output: Path | None = None,
    coaches_output: Path | None = None,
) -> tuple[Path, Path]:
    """Scrape rosters + coaches for the given NCAA team ids; resumable per-team CSVs."""
    roster_out = Path(roster_output) if roster_output else (
        settings.staging_dir / f"ncaa_wvb_rosters_d1_{year}.csv"
    )
    coach_out = Path(coaches_output) if coaches_output else (
        settings.staging_dir / f"ncaa_wvb_coaches_d1_{year}.csv"
    )
    roster_out.parent.mkdir(parents=True, exist_ok=True)

    meta = season_team_ids(year)
    done: set[str] = set()
    if roster_out.exists():
        try:
            done = set(pd.read_csv(roster_out, usecols=["TeamID"], dtype={"TeamID": str})["TeamID"])
            log.info("[resume] %d team(s) already in rosters CSV; skipping.", len(done))
        except Exception:
            pass

    team_ids = [str(t) for t in team_ids]
    for i, tid in enumerate(team_ids, 1):
        if tid in done:
            continue
        entry = meta.get(tid, {})
        team_name = entry.get("team") or entry.get("short_name") or ""
        conference = entry.get("conference", "")
        season_label = f"{year}-{year + 1}"
        html = fetch_html(f"https://stats.ncaa.org/teams/{tid}/roster", wait_selectors=("table",))
        rdf = _extract_roster_table(html)
        n = 0
        if rdf is not None and not rdf.empty:
            rdf.insert(0, "Season", season_label)
            rdf.insert(1, "TeamID", tid)
            rdf.insert(2, "Team", team_name)
            rdf.insert(3, "Conference", conference)
            cols = [c for c in ROSTER_COLS if c in rdf.columns]
            rdf[cols].to_csv(roster_out, mode="a", header=not roster_out.exists(), index=False)
            n = len(rdf)
        coach = _extract_coach(html, tid, team_name)
        if coach:
            pd.DataFrame([coach]).to_csv(
                coach_out, mode="a", header=not coach_out.exists(), index=False
            )
        log.info("[roster %d/%d] team_id=%s players=%d", i, len(team_ids), tid, n)
    return roster_out, coach_out
