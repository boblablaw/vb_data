"""Team schedule scraping from stats.ncaa.org team pages.

Each ``/teams/<id>`` page's first table is the team's full schedule — past results *and* upcoming
games. Played games are already captured as ``contests`` (with box scores) by the game-stats
scrape; this scraper exists for the UPCOMING games, which have no ``contest_id`` yet and name the
opponent as a display string. One page load per team; resumable per-team CSV, mirroring rosters.py.

Opponent cell conventions (verified live):
  * ``@ X``            -> away game at X
  * ``X @ City, ST``   -> neutral-site game vs X
  * ``X``              -> home game vs X
A leading rank (``#5``) and a trailing record (``(5-0)``) are stripped from the opponent name.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from ..config import settings
from ..fetch import fetch_html
from ..log import get_logger

log = get_logger(__name__)

SCHEDULE_COLS = [
    "Season", "TeamNcaaId", "Date", "Time", "OpponentName",
    "OpponentNcaaId", "Site", "NeutralLocation", "ResultRaw", "ContestId",
]

_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}:\d{2}\s*[AP]M))?", re.IGNORECASE)
_TEAM_HREF_RE = re.compile(r"/teams/(\d+)")
# NCAA pre-creates a contest (with its permanent game id) for every scheduled game and links the
# schedule row to it, so we can capture the id here — the same id that serves as ncaa.com/game/<id>
# and, once the box score is scraped, as the ``contests`` primary key. Matches with or without the
# ``/box_score`` suffix (played rows link to the box score; upcoming rows to the contest itself).
_CONTEST_HREF_RE = re.compile(r"/contests/(\d+)")
_RANK_PREFIX_RE = re.compile(r"^(?:#\s*\d+|RV|NR)\s+", re.IGNORECASE)
_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _clean_opponent(name: str) -> str:
    name = _RANK_PREFIX_RE.sub("", name.strip())
    name = _TRAILING_PAREN_RE.sub("", name)
    return name.strip()


def _parse_opponent(cell_text: str) -> tuple[str, str, str | None]:
    """Parse an opponent cell into (site, opponent_name, neutral_location)."""
    text = " ".join(cell_text.split())
    if text.startswith("@"):
        return "away", _clean_opponent(text[1:]), None
    if " @" in text:
        name, loc = text.split(" @", 1)
        return "neutral", _clean_opponent(name), loc.strip() or None
    return "home", _clean_opponent(text), None


def _parse_schedule_rows(html: str) -> list[dict]:
    """Extract schedule rows (any table row whose first cell carries a date) from a team page."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for table in soup.find_all("table"):
        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue
            m = _DATE_RE.search(cells[0].get_text(" ", strip=True))
            if not m:
                continue
            try:
                iso_date = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                continue
            game_time = (m.group(2) or "").upper().replace("  ", " ").strip()

            opp_cell = cells[1]
            site, opp_name, neutral = _parse_opponent(opp_cell.get_text(" ", strip=True))
            if not opp_name:
                continue
            href = opp_cell.find("a", href=_TEAM_HREF_RE)
            opp_id = ""
            if href:
                hm = _TEAM_HREF_RE.search(href.get("href", ""))
                if hm:
                    opp_id = hm.group(1)
            result = cells[2].get_text(" ", strip=True) if len(cells) > 2 else ""

            # Any /contests/<id> link in the row (result cell for played games, the matchup link
            # for upcoming ones) — the game's permanent NCAA id, used to link out to ncaa.com.
            contest_id = ""
            chref = tr.find("a", href=_CONTEST_HREF_RE)
            if chref:
                cm = _CONTEST_HREF_RE.search(chref.get("href", ""))
                if cm:
                    contest_id = cm.group(1)

            key = (iso_date, opp_name.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "Date": iso_date, "Time": game_time, "OpponentName": opp_name,
                "OpponentNcaaId": opp_id, "Site": site, "NeutralLocation": neutral or "",
                "ResultRaw": result, "ContestId": contest_id,
            })
    return out


def scrape_schedule(
    team_ids: Iterable[str], year: int, output: Path | None = None
) -> Path:
    """Scrape team schedules for the given NCAA team ids; resumable per-team CSV."""
    out = Path(output) if output else (
        settings.staging_dir / f"ncaa_wvb_schedule_d1_{year}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    done: set[str] = set()
    if out.exists():
        try:
            done = set(
                pd.read_csv(out, usecols=["TeamNcaaId"], dtype={"TeamNcaaId": str})["TeamNcaaId"]
            )
            log.info("[resume] %d team(s) already in schedule CSV; skipping.", len(done))
        except Exception:
            pass

    team_ids = [str(t) for t in team_ids]
    season_label = f"{year}-{year + 1}"
    for i, tid in enumerate(team_ids, 1):
        if tid in done:
            continue
        try:
            html = fetch_html(f"https://stats.ncaa.org/teams/{tid}", wait_selectors=("table",))
            rows = _parse_schedule_rows(html)
        except Exception as e:
            log.warning("[schedule %d/%d] team_id=%s failed, skipping: %s",
                        i, len(team_ids), tid, e)
            continue
        if rows:
            df = pd.DataFrame(rows)
            df.insert(0, "Season", season_label)
            df.insert(1, "TeamNcaaId", tid)
            df[SCHEDULE_COLS].to_csv(out, mode="a", header=not out.exists(), index=False)
        log.info("[schedule %d/%d] team_id=%s games=%d", i, len(team_ids), tid, len(rows))
    return out
