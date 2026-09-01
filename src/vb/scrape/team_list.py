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
from ..log import get_logger

log = get_logger(__name__)

LIST_URL = (
    "https://stats.ncaa.org/team/inst_team_list"
    "?academic_year={academic_year}&conf_id={conf_id}&division={division}&sport_code=WVB"
)
LINK_RE = re.compile(r'/teams/(\d+)"[^>]*>\s*([^<]+?)\s*<')

# The conference <select> on inst_team_list. Isolate that dropdown first (there are other
# selects: division, academic_year, sport), then read its <option value=id>Name</option> rows.
_CONF_SELECT_RE = re.compile(r'<select[^>]*name="conf_id"[^>]*>(.*?)</select>', re.DOTALL | re.IGNORECASE)
_OPTION_RE = re.compile(r'<option[^>]*value="(-?\d+)"[^>]*>\s*(.*?)\s*</option>', re.DOTALL | re.IGNORECASE)


def _list_url(year: int, division: int, conf_id: int) -> str:
    return LIST_URL.format(academic_year=year + 1, conf_id=conf_id, division=division)


def _parse_team_links(html: str) -> dict[str, str]:
    """{team_id -> team_name} from the /teams/<id> anchors on an inst_team_list page."""
    seen: dict[str, str] = {}
    for tid, name in LINK_RE.findall(html):
        name = _html.unescape(name).strip()
        if tid and name and tid not in seen:
            seen[tid] = name
    return seen


def fetch_team_list(year: int, division: int = 1, conf_id: int = -1) -> list[dict]:
    """All WVB teams for a season (conf_id=-1), or one conference's teams."""
    html = fetch_html(_list_url(year, division, conf_id),
                      wait_selectors=("a[href^='/teams/']", "table"))
    return [{"team_id": tid, "team_name": name, "div": division, "yr": year}
            for tid, name in _parse_team_links(html).items()]


def fetch_conference_options(year: int, division: int = 1) -> dict[int, str]:
    """{conf_id -> conference name} from the page's conference dropdown (drops the 'All' entry)."""
    html = fetch_html(_list_url(year, division, -1), wait_selectors=('select[name="conf_id"]',))
    m = _CONF_SELECT_RE.search(html)
    if not m:
        raise RuntimeError(
            "conf_id <select> not found on inst_team_list — page markup changed or was blocked."
        )
    confs: dict[int, str] = {}
    for cid, raw in _OPTION_RE.findall(m.group(1)):
        cid = int(cid)
        name = _html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if cid > 0 and name and name.lower() not in ("all", "all conferences"):
            confs[cid] = name
    if not confs:
        raise RuntimeError("conf_id dropdown parsed but yielded no conferences.")
    return confs


def fetch_conference_membership(year: int, division: int = 1) -> dict[str, tuple[str, str]]:
    """Authoritative {team_id -> (conference_name, team_name)} for the season, straight from NCAA.

    Reads the conference dropdown, then fetches each conference's filtered team list. This is the
    same source the pipeline trusts for team ids, so it's the gold-standard cross-check for the
    hand-maintained ``teams.json`` conference field.
    """
    confs = fetch_conference_options(year, division)
    out: dict[str, tuple[str, str]] = {}
    for i, (cid, cname) in enumerate(sorted(confs.items()), 1):
        html = fetch_html(_list_url(year, division, cid),
                          wait_selectors=("a[href^='/teams/']", "table"))
        teams = _parse_team_links(html)
        log.info("[conf %d/%d] %s (id=%s): %d teams", i, len(confs), cname, cid, len(teams))
        for tid, tname in teams.items():
            out[tid] = (cname, tname)
    return out


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
