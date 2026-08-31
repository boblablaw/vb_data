"""Harvest the NCAA D1 WVB team list for a season from stats.ncaa.org.

Year semantics (THE gotcha): ``year`` is the SEASON (fall) year, matching teams.json's
ncaa_team_ids keys. NCAA labels academic years by their ENDING year, so the fall-<year>
season lives at academic_year = <year> + 1. Ported from the original fetch_ncaa_team_list.
"""
from __future__ import annotations

import csv
import html as _html
import re
from pathlib import Path

from ..config import settings
from ..fetch import fetch_html

LIST_URL = (
    "https://stats.ncaa.org/team/inst_team_list"
    "?academic_year={academic_year}&conf_id=-1&division={division}&sport_code=WVB"
)
LINK_RE = re.compile(r'/teams/(\d+)"[^>]*>\s*([^<]+?)\s*<')


def fetch_team_list(year: int, division: int = 1) -> list[dict]:
    academic_year = year + 1  # season (fall) year -> NCAA academic_year
    url = LIST_URL.format(academic_year=academic_year, division=division)
    html = fetch_html(url, wait_selectors=("a[href^='/teams/']", "table"))
    seen: dict[str, str] = {}
    for tid, name in LINK_RE.findall(html):
        name = _html.unescape(name).strip()
        if tid and name and tid not in seen:
            seen[tid] = name
    return [{"team_id": tid, "team_name": name, "div": division, "yr": year}
            for tid, name in seen.items()]


def scrape_team_list(year: int, division: int = 1, output: Path | None = None) -> Path:
    rows = fetch_team_list(year, division)
    if not rows:
        raise SystemExit("No teams parsed — page may be blocked or empty.")
    out = Path(output) if output else settings.exports_dir / f"ncaa_wvb_team_list_{year}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["team_id", "team_name", "div", "yr"])
        w.writeheader()
        w.writerows(rows)
    return out
