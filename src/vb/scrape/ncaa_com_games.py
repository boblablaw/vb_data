"""Fetch ncaa.com's public scoreboard for a date to recover each game's ncaa.com game id.

ncaa.com/game/<id> uses a DIFFERENT id system from stats.ncaa.org's ``contest_id`` (our PK), so we
can't build a public-site link from the ids we already have. This module hits ncaa.com's own
GraphQL scoreboard endpoint (``GetContests_web``) — one plain-HTTP request per date, NOT Akamai-
protected, so no Playwright/real-Chrome is needed — and returns each game's ncaa.com id plus its
team seonames and date so the loader can match it to our games on (date + team pair).

The persisted-query hash is baked in but can drift if ncaa.com redeploys their frontend; on a
``PersistedQueryNotFound`` response we re-scrape the current hash from the scoreboard HTML.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date as _date

import requests

from ..log import get_logger

log = get_logger(__name__)

_GRAPHQL_URL = "https://sdataprod.ncaa.com/"
_SCOREBOARD_HTML = "https://www.ncaa.com/scoreboard/volleyball-women/d1/{y}/{m:02d}/{d:02d}/all-conf"
# Current GetContests_web persisted-query hash (see module docstring for the drift fallback).
_GET_CONTESTS_HASH = "4bcb5e6432fa9da365c0c19af01b1f9015cc7eb5c21e7af2dba308784a166df7"
_HASH_RE = re.compile(r"GetContests_web[^}]*?sha256Hash%22%3A%22([0-9a-f]{64})%22")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.ncaa.com/",
    "Accept": "application/json",
}
_TIMEOUT = 30


@dataclass(frozen=True)
class NcaaComGame:
    """One ncaa.com scoreboard game: its public id, ISO date, and the two teams' slugs/names."""
    ncaa_game_id: str
    date: str                     # ISO YYYY-MM-DD
    seonames: tuple[str, ...]     # ncaa.com team slugs, e.g. ("michigan-st", "south-carolina")
    name_shorts: tuple[str, ...]  # display names, e.g. ("Michigan St.", "South Carolina")
    start_epoch: int | None       # UTC epoch seconds, or None/0 when unset
    game_state: str | None        # "F" final, "P" pregame/upcoming, etc.


def _request(season: int, mmddyyyy: str, sha: str) -> requests.Response:
    # Compact JSON is required: the persisted-query middleware fails to parse ``extensions`` when
    # url-encoded spaces (``+``) sit inside the JSON, replying 500 "Must specify sha256Hash".
    return requests.get(
        _GRAPHQL_URL,
        params={
            "meta": "GetContests_web",
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": sha}}, separators=(",", ":")
            ),
            "variables": json.dumps(
                {"sportCode": "WVB", "division": 1, "seasonYear": season, "contestDate": mmddyyyy},
                separators=(",", ":"),
            ),
        },
        headers=_HEADERS,
        timeout=_TIMEOUT,
    )


def _current_hash(day: _date) -> str | None:
    """Re-scrape the live GetContests_web hash from the scoreboard HTML (drift fallback)."""
    url = _SCOREBOARD_HTML.format(y=day.year, m=day.month, d=day.day)
    try:
        html = requests.get(url, headers={**_HEADERS, "Accept": "text/html"}, timeout=_TIMEOUT).text
    except requests.RequestException as e:  # pragma: no cover - network hiccup
        log.warning("ncaa.com hash refresh failed for %s: %s", day, e)
        return None
    m = _HASH_RE.search(html)
    return m.group(1) if m else None


def fetch_games(day: _date, season: int) -> list[NcaaComGame]:
    """Return all D1 WVB games ncaa.com lists for ``day`` (empty on an off day or a fetch error)."""
    mmddyyyy = day.strftime("%m/%d/%Y")
    resp = _request(season, mmddyyyy, _GET_CONTESTS_HASH)
    payload: dict = {}
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    # Hash drift -> ncaa.com replies 200 with an errors[] block; re-scrape and retry once.
    if resp.status_code != 200 or "errors" in payload:
        fresh = _current_hash(day)
        if fresh and fresh != _GET_CONTESTS_HASH:
            log.info("ncaa.com persisted-query hash drifted; using refreshed hash %s", fresh[:12])
            resp = _request(season, mmddyyyy, fresh)
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
    contests = ((payload.get("data") or {}).get("contests")) or []
    out: list[NcaaComGame] = []
    for g in contests:
        gid = g.get("contestId")
        teams = g.get("teams") or []
        if gid is None or len(teams) != 2:
            continue
        iso = _iso_date(g.get("startDate"), day)
        out.append(NcaaComGame(
            ncaa_game_id=str(gid),
            date=iso,
            seonames=tuple((t.get("seoname") or "") for t in teams),
            name_shorts=tuple((t.get("nameShort") or "") for t in teams),
            start_epoch=(int(g["startTimeEpoch"]) if g.get("startTimeEpoch") else None),
            game_state=g.get("gameState"),
        ))
    return out


def _iso_date(startdate: str | None, fallback: _date) -> str:
    """ncaa.com's ``startDate`` is "MM/DD/YYYY"; fall back to the requested day if it's missing."""
    if startdate:
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", startdate)
        if m:
            mo, dd, yy = (int(x) for x in m.groups())
            return f"{yy:04d}-{mo:02d}-{dd:02d}"
    return fallback.isoformat()
