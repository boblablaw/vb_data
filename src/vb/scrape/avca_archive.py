"""Fetch historical AVCA Coaches Poll (Division I women) weeks from avca.org.

The live enrichment (:func:`vb.load.enrichment.enrich_avca`) only sees *this week's* poll from
ncaa.com, so rank history (``ranking_snapshots``) can only accumulate going forward. This module
backfills past weeks from avca.org's own archive, which is a plain server-rendered WordPress/FacetWP
site: the poll for a given season+week is selected purely by querystring, so plain HTTP works (no JS,
no Akamai gate).

Each poll page carries its release date in ``<h2 class="dynamic-title">`` (e.g. "Oct. 20 AVCA/…") and
a single 25-row table. There is no year in the title, but every poll (preseason in August through the
final poll in December) falls in the season's own calendar year, so ``as_of`` uses the season year.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as date_cls
from io import StringIO

import pandas as pd
import requests

from ..log import get_logger

log = get_logger(__name__)

ARCHIVE_URL = "https://www.avca.org/polls-awards/polls/"
DIVISION = "division-i-women"
# FacetWP week term slugs, in chronological order. Weeks past a season's actual length simply return
# no table and are skipped, so the upper bound is a safe over-estimate.
WEEK_SLUGS = ["preseason", *[f"week-{i}" for i in range(1, 18)], "final"]

_UA = {"User-Agent": "Mozilla/5.0 (compatible; vb-rankings/1.0)"}
_TITLE_RE = re.compile(r'<h2[^>]*class="[^"]*dynamic-title[^"]*"[^>]*>\s*([^<]+)', re.IGNORECASE)
# Leading "<Mon>. <day>" in a poll title, e.g. "Oct. 20 AVCA/TARAFLEX …" or "Sept. 8 …".
_DATE_RE = re.compile(r"^\s*([A-Za-z]+)\.?\s+(\d{1,2})\b")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# First-place-vote suffix on the school cell — brackets on avca.org ("Nebraska [58]"), parens on some
# mirrors ("Nebraska (58)").
_VOTE_SUFFIX = re.compile(r"\s*[\[(]\d+[\])]\s*$")


@dataclass
class AvcaPoll:
    week: str                       # FacetWP slug, e.g. "week-8" / "final"
    as_of: date_cls                 # poll release date
    title: str                      # full dynamic-title, e.g. "Oct. 20 AVCA/TARAFLEX Division I WVB Poll"
    ranks: list[tuple[int, str]]    # [(rank, school_name), …] with vote suffixes stripped


def _parse_date(title: str, season: int) -> date_cls | None:
    m = _DATE_RE.search(title)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    try:
        return date_cls(season, mon, int(m.group(2)))
    except ValueError:
        return None


def fetch_poll(season: int, week: str, *, session: requests.Session | None = None) -> AvcaPoll | None:
    """Fetch and parse one season+week poll; None if that week has no published poll."""
    getter = session or requests
    try:
        resp = getter.get(
            ARCHIVE_URL,
            params={"_season": season, "_divisions": DIVISION, "_weeks": week},
            headers=_UA,
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:  # network hiccup on one week shouldn't abort the whole season
        log.warning("avca_archive: fetch failed for %s %s: %s", season, week, e)
        return None

    html = resp.text
    tm = _TITLE_RE.search(html)
    title = tm.group(1).strip() if tm else ""
    as_of = _parse_date(title, season)

    try:
        tables = pd.read_html(StringIO(html))
    except ValueError:
        return None  # no table on the page → week not published
    if not tables:
        return None
    df = tables[0]
    cols = {str(c).strip().lower(): c for c in df.columns}
    rank_col = next((cols[c] for c in cols if "rank" in c and "previous" not in c), None)
    team_col = next((cols[c] for c in cols if "school" in c or "team" in c or "institution" in c), None)
    if rank_col is None or team_col is None or as_of is None:
        log.warning("avca_archive: %s %s missing rank/team column or date (title=%r)", season, week, title)
        return None

    ranks: list[tuple[int, str]] = []
    for _, r in df.iterrows():
        name = _VOTE_SUFFIX.sub("", str(r[team_col]).strip())
        try:
            rank = int(str(r[rank_col]).strip())
        except (ValueError, TypeError):
            continue
        if name:
            ranks.append((rank, name))
    if not ranks:
        return None
    return AvcaPoll(week=week, as_of=as_of, title=title, ranks=ranks)


def fetch_season_polls(season: int) -> list[AvcaPoll]:
    """Every published AVCA poll for a season, oldest first, de-duplicated by release date.

    A given release date can be reachable under more than one week slug in the facet; keeping the
    first occurrence (chronological slug order) collapses those to one poll per date.
    """
    sess = requests.Session()
    polls: list[AvcaPoll] = []
    seen: set[date_cls] = set()
    for week in WEEK_SLUGS:
        poll = fetch_poll(season, week, session=sess)
        if poll is None or poll.as_of in seen:
            continue
        seen.add(poll.as_of)
        polls.append(poll)
        log.info("avca_archive: %s %s → %s (%d teams)", season, week, poll.as_of, len(poll.ranks))
    polls.sort(key=lambda p: p.as_of)
    return polls
