"""Client for our self-hosted ncaa.com wrapper (henrygd/ncaa-api).

ncaa.com is a DIFFERENT host from the Akamai-blocked ``stats.ncaa.org``, so this sidecar is the
**resilient primary** for schedules, box scores, and lineups: it keeps working even when
stats.ncaa.org IP-blocks the box. The service is a thin JSON wrapper over ncaa.com's own data — plain
HTTP, no Playwright/real-Chrome needed — reached at ``settings.ncaa_api_base_url``
(``http://127.0.0.1:3013`` from host scrapers, ``http://ncaa-api:3000`` from the vb-api container).

Endpoints used (WVB):
  - ``/scoreboard/volleyball-women/d1/YYYY/MM/DD/all-conf`` -> games for a date
  - ``/game/<gameID>/boxscore``                            -> per-player stat lines + starter flags
  - ``/game/<gameID>/play-by-play``                        -> rally-level play text per set

The boxscore is the big win: it returns full per-player WVB lines *with jersey number and a
``starter`` flag*, so it feeds both ``player_game_stats`` and lineups directly — no PBP
reconstruction. Player-id reconciliation is by ``(team, jersey number, name)`` (see load/ncaa_api.py).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import requests

from ..config import settings
from ..log import get_logger

log = get_logger(__name__)

_SPORT_PATH = "volleyball-women/d1"
_TIMEOUT = 30
_MAX_RETRIES = 4
_MAX_BACKOFF_SECONDS = 20.0
_UA = "vb_data-ncaa-api/1.0 (personal NCAA volleyball stats project)"


# --- return shapes --------------------------------------------------------------------------------

@dataclass(frozen=True)
class ApiGame:
    """One scoreboard game from henrygd: ncaa.com id, ISO date, the two teams' slugs/names, state."""
    ncaa_game_id: str
    date: str                     # ISO YYYY-MM-DD
    seonames: tuple[str, ...]     # ncaa.com team slugs, e.g. ("hawaii", "san-jose-st")
    name_shorts: tuple[str, ...]  # display names, e.g. ("Hawaii", "San Jose St.")
    start_epoch: int | None       # UTC epoch seconds, or None when unset/0
    game_state: str | None        # "final", "live", "pre", ...


@dataclass(frozen=True)
class ApiPlayerLine:
    """One player's box-score line for a game, from a team's ``playerStats`` entry.

    Stat fields carry ncaa.com's names; the loader maps them to ``PlayerGameStat`` columns. Counting
    stats are floats (parsed from strings); ``None`` when henrygd omitted the field.
    """
    seoname: str                  # the team's ncaa.com slug this line belongs to
    ncaa_team_id: str | None      # ncaa.com's numeric team id (henrygd ``teamId``)
    first_name: str
    last_name: str
    number: int | None            # jersey number
    position: str | None
    starter: bool
    participated: bool
    # counting stats (mapped to PlayerGameStat in the loader)
    games_played: float | None    # sets played -> PlayerGameStat.sets
    kills: float | None
    attack_errors: float | None   # -> errors
    attack_attempts: float | None  # -> total_attacks
    assists: float | None
    service_aces: float | None    # -> aces
    service_errors: float | None  # -> serr
    digs: float | None
    reception_attempts: float | None  # -> retatt
    reception_errors: float | None    # -> rerr
    block_solos: float | None
    block_assists: float | None
    blocking_errors: float | None  # -> berr
    ball_handling_errors: float | None  # -> bhe
    points: float | None          # -> pts

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass(frozen=True)
class ApiBoxscore:
    """A game's box score: the two teams (by seoname) and every player's line."""
    ncaa_game_id: str
    lines: list[ApiPlayerLine] = field(default_factory=list)


@dataclass(frozen=True)
class ApiSetStarters:
    """The starters one team fielded for one set, from ncaa.com's PBP ("Team starters: A, B, ...")."""
    set_number: int
    seoname: str                  # the team's ncaa.com slug
    ncaa_team_id: str | None      # henrygd numeric team id
    player_names: tuple[str, ...]  # the six rotation starters (libero excluded), verbatim


@dataclass(frozen=True)
class ApiPlayByPlay:
    """A game's play-by-play, parsed for the per-set explicit starter lists."""
    ncaa_game_id: str
    set_starters: list[ApiSetStarters] = field(default_factory=list)


class NcaaApiError(RuntimeError):
    """The ncaa-api sidecar was unreachable or returned an error/blank response."""


# --- HTTP -----------------------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    s.headers["Accept"] = "application/json"
    return s


def _get(path: str, *, session: requests.Session | None = None) -> dict:
    """GET ``<base>/<path>`` as JSON, retrying transient failures with capped exponential backoff."""
    sess = session or _session()
    url = f"{settings.ncaa_api_base_url.rstrip('/')}/{path.lstrip('/')}"
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = sess.get(url, timeout=_TIMEOUT)
            # henrygd relays ncaa.com hiccups as 429/5xx; retry those, fail fast on 4xx.
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES - 1:
                wait = min(float(resp.headers.get("Retry-After") or 0) or 2.0 * (attempt + 1),
                           _MAX_BACKOFF_SECONDS)
                log.info("ncaa-api %s -> %d; backing off %.0fs (attempt %d)",
                         path, resp.status_code, wait, attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:  # ValueError = bad JSON
            last_exc = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(min(2.0 * (attempt + 1), _MAX_BACKOFF_SECONDS))
                continue
    raise NcaaApiError(f"ncaa-api GET {url} failed: {last_exc}") from last_exc


# --- parsing helpers ------------------------------------------------------------------------------

def _f(v) -> float | None:
    """Parse henrygd's stringy stat value to float; blank/missing -> None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


# --- public API -----------------------------------------------------------------------------------

def scoreboard(day, *, session: requests.Session | None = None) -> list[ApiGame]:
    """All D1 WVB games on ``day`` (a ``datetime.date``)."""
    path = f"scoreboard/{_SPORT_PATH}/{day.year}/{day.month:02d}/{day.day:02d}/all-conf"
    data = _get(path, session=session)
    out: list[ApiGame] = []
    for wrap in data.get("games", []):
        g = wrap.get("game", wrap)
        home, away = g.get("home", {}), g.get("away", {})
        hn, an = home.get("names", {}), away.get("names", {})
        gid = str(g.get("gameID") or "").strip()
        if not gid:
            continue
        out.append(ApiGame(
            ncaa_game_id=gid,
            date=_iso_date(g.get("startDate")),
            seonames=(an.get("seo", ""), hn.get("seo", "")),
            name_shorts=(an.get("short", ""), hn.get("short", "")),
            start_epoch=_i(g.get("startTimeEpoch")) or None,
            game_state=(g.get("gameState") or None),
        ))
    return out


def _iso_date(mdY: str | None) -> str:
    """henrygd startDate is 'MM/DD/YYYY'; return ISO 'YYYY-MM-DD' ('' if unparseable)."""
    if not mdY:
        return ""
    parts = mdY.split("/")
    if len(parts) != 3:
        return ""
    m, d, y = parts
    return f"{y}-{int(m):02d}-{int(d):02d}" if y.isdigit() else ""


def boxscore(ncaa_game_id: str, *, session: requests.Session | None = None) -> ApiBoxscore:
    """Per-player WVB box-score lines for a game (both teams), keyed by team seoname."""
    data = _get(f"game/{ncaa_game_id}/boxscore", session=session)
    # teamId -> seoname, from the "teams" block (playerStats groups carry only the numeric teamId).
    seo_by_id = {str(t.get("teamId")): (t.get("seoname") or "") for t in data.get("teams", [])}
    lines: list[ApiPlayerLine] = []
    for team in data.get("teamBoxscore", []):
        tid = str(team.get("teamId"))
        seo = seo_by_id.get(tid, "")
        for p in team.get("playerStats", []):
            lines.append(ApiPlayerLine(
                seoname=seo,
                ncaa_team_id=tid or None,
                first_name=(p.get("firstName") or "").strip(),
                last_name=(p.get("lastName") or "").strip(),
                number=_i(p.get("number")),
                position=(p.get("position") or None),
                starter=bool(p.get("starter")),
                participated=bool(p.get("participated")),
                games_played=_f(p.get("gamesPlayed")),
                kills=_f(p.get("kills")),
                attack_errors=_f(p.get("attackErrors")),
                attack_attempts=_f(p.get("attackAttempts")),
                assists=_f(p.get("assists")),
                service_aces=_f(p.get("serviceAces")),
                service_errors=_f(p.get("serviceErrors")),
                digs=_f(p.get("digs")),
                reception_attempts=_f(p.get("receptionAttempts")),
                reception_errors=_f(p.get("receptionErrors")),
                block_solos=_f(p.get("blockSolos")),
                block_assists=_f(p.get("blockAssists")),
                blocking_errors=_f(p.get("blockingErrors")),
                ball_handling_errors=_f(p.get("ballHandlingErrors")),
                points=_f(p.get("points")),
            ))
    return ApiBoxscore(ncaa_game_id=str(ncaa_game_id), lines=lines)


# "<Team> starters: Name1; Name2; ..." — capture everything after "starters:". ncaa.com delimits
# with semicolons (older/other feeds use commas), and the last name often carries a trailing period.
_STARTERS_RE = re.compile(r"starters:\s*(.+)$", re.IGNORECASE)
_NAME_SPLIT_RE = re.compile(r"[;,]")


def play_by_play(ncaa_game_id: str, *, session: requests.Session | None = None) -> ApiPlayByPlay:
    """Parse a game's PBP for each set's explicit starter list (per team).

    ncaa.com's PBP opens every set with a "<Team> starters: A, B, C, D, E, F" line; henrygd groups
    plays by numeric teamId under each period (= set). We map teamId -> seoname via the top-level
    ``teams`` block and pull the six names out of each starters line.
    """
    data = _get(f"game/{ncaa_game_id}/play-by-play", session=session)
    seo_by_id = {str(t.get("teamId")): (t.get("seoname") or "") for t in data.get("teams", [])}
    out: list[ApiSetStarters] = []
    for per in data.get("periods", []):
        try:
            sn = int(per.get("periodNumber"))
        except (TypeError, ValueError):
            continue
        for grp in per.get("playbyplayStats", []):
            tid = str(grp.get("teamId"))
            for pl in grp.get("plays", []):
                m = _STARTERS_RE.search(pl.get("playText") or "")
                if not m:
                    continue
                names = tuple(
                    nm for n in _NAME_SPLIT_RE.split(m.group(1))
                    if (nm := n.strip().rstrip(".").strip())
                )
                if names:
                    out.append(ApiSetStarters(
                        set_number=sn, seoname=seo_by_id.get(tid, ""),
                        ncaa_team_id=tid or None, player_names=names,
                    ))
                break  # at most one starters line per team-group
    return ApiPlayByPlay(ncaa_game_id=str(ncaa_game_id), set_starters=out)
