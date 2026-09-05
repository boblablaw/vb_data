"""Play-by-play (touch-level) events from stats.ncaa.org — /contests/<id>/play_by_play.

Each contest's PBP page renders, per set, one HTML row per *touch* (serve / reception / set /
attack / dig / block), one row per substitution, and — for the touch that ends a rally — a
*terminal* row carrying a ``<span class="short_play_text">`` with the correctly-labelled outcome
(Kill / Ace / Block / *error*) and the running score. A touch/sub row sits in the *away* (left)
or *home* (right) ``<td>``; the middle ``<td>`` holds the running score on terminal rows only.

The parser turns that into flat per-touch event rows (mirroring ``scrape/game_stats.py``):

  * **detail touches** — ``touch_type`` in {serve, reception, set, attack, dig, block},
    ``is_terminal`` False, ``side`` = the team that made the touch.
  * **subs** — ``touch_type`` sub_in / sub_out, ``side`` = the subbing team.
  * **terminals** — ``touch_type`` "terminal" with ``terminal_type`` (kill/ace/block/
    attack_error/service_error/reception_error/set_error/ball_handling_error/other), the running
    score, and ``scoring_side``. ``side`` is the credited team: the scoring side for
    kill/ace/block, the erroring side (the *other* team) for any ``*_error``.

Terminals are stored as their OWN rows (``touch_type='terminal'``), never as ``attack``/``block``,
so counting ``touch_type='attack'`` gives attack *attempts* without double-counting the swing that
became a kill (the kill's own "Attack by" detail row is still present and counted once).

Validated against contest 6595050 (LMU @ Hawaii): reconstructs the exact set scores and the
205 terminals reconcile to the 3-2 final (away 92 / home 113).

Writes a resumable CSV (staging/ncaa_wvb_pbp_d1_<year>.csv); contests already present are skipped.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import lxml.html
import pandas as pd

from ..config import settings
from ..fetch import fetch_html
from ..log import get_logger
from .game_stats import (
    TEAM_LINK_RE,
    _append_df,
    _existing_contest_ids,
    discover_contests,
    discover_contests_by_date,
)

log = get_logger(__name__)

# One touch/sub per row, text in the away (td[0]) or home (td[2]) cell; score in td[1] on terminals.
_SERVE_TO_RE = re.compile(r"^(.+?) serves to (.+)$", re.IGNORECASE)
_SERVE_RE = re.compile(r"^(.+?) serves(?:\s+an\s+ace)?$", re.IGNORECASE)
_BY_RE = re.compile(r"^(.+?) by (.+)$", re.IGNORECASE)  # "Reception by X", "Kill by X", ...
_SUB_RE = re.compile(r"^Sub (in|out) (.+)$", re.IGNORECASE)
_SCORE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
_ATTEND_RE = re.compile(r"Attendance:\s*([\d,]+)", re.IGNORECASE)
_DATE_ROW_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}\b")

# Non-touch marker rows to skip outright.
_SKIP_EXACT = {"Match started", "Set started", "Match ended", "Set ended"}

# CSV column order (also the DataFrame schema).
COLUMNS = [
    "ContestID", "Season", "Set", "Rally", "Seq", "TouchType", "PlayerName", "Side",
    "IsTerminal", "TerminalType", "ScoringSide", "AwayScore", "HomeScore",
    "AwayNcaaId", "HomeNcaaId", "Location", "Attendance",
]


def _norm(s: str | None) -> str:
    """Collapse whitespace and drop the NBSP / SOH glyphs NCAA sprinkles into cells."""
    return " ".join((s or "").replace("\xa0", " ").replace("\x01", " ").split())


def _terminal_type(text: str) -> str:
    """Normalized outcome for a terminal (short_play_text) row. Order matters (errors first)."""
    t = text.lower()
    if "ball handling error" in t:
        return "ball_handling_error"
    if "attack error" in t:
        return "attack_error"
    if "service error" in t:
        return "service_error"
    if "reception error" in t:
        return "reception_error"
    if "set error" in t:
        return "set_error"
    if "block error" in t:
        return "block_error"
    if "kill" in t:                       # incl. "first ball kill"
        return "kill"
    if "ace" in t:                        # "serves an ace" / "service ace"
        return "ace"
    if "block" in t:
        return "block"
    return "other"


def _touch_type(text: str) -> str | None:
    """Skill for a detail (non-terminal) touch row, or None if it isn't a touch row."""
    if _SERVE_TO_RE.match(text) or _SERVE_RE.match(text):
        return "serve"
    m = _BY_RE.match(text)
    if not m:
        return None
    verb = m.group(1).lower()
    for skill in ("reception", "set", "attack", "dig", "block"):
        if verb.startswith(skill):
            return skill
    return None


def _player(text: str) -> str | None:
    m = _BY_RE.match(text)
    if m:
        return _norm(m.group(2))
    m = _SERVE_TO_RE.match(text) or _SERVE_RE.match(text)
    return _norm(m.group(1)) if m else None


def parse_venue_attendance(html: str) -> tuple[str | None, int | None]:
    """(location, attendance) from the linescore header's grey_text colspan-7 rows.

    Under the linescore sit up to three ``<td class="grey_text" colspan="7">`` cells: the date,
    the venue ("Arena (City, ST)"), and "Attendance: N". Either may be absent.
    """
    doc = lxml.html.fromstring(html)
    location: str | None = None
    attendance: int | None = None
    for td in doc.xpath('//td[@colspan="7" and contains(@class, "grey_text")]'):
        text = _norm(td.text_content())
        if not text:
            continue
        m = _ATTEND_RE.search(text)
        if m:
            attendance = int(m.group(1).replace(",", ""))
        elif _DATE_ROW_RE.match(text):
            continue  # date row, handled elsewhere
        elif location is None:
            location = text
    return location, attendance


def parse_pbp(html: str, contest_id: str, season: int) -> tuple[list[dict], dict]:
    """Parse a PBP page into (event rows, contest meta).

    ``event rows`` are dicts keyed by :data:`COLUMNS`; ``meta`` carries away/home NCAA ids,
    location, and attendance. A page with no set tables yields ``([], meta)``.
    """
    doc = lxml.html.fromstring(html)
    ids: list[str] = []
    for tid in TEAM_LINK_RE.findall(html):
        if tid not in ids:
            ids.append(tid)
    location, attendance = parse_venue_attendance(html)
    meta = {
        "AwayNcaaId": ids[0] if len(ids) >= 1 else None,
        "HomeNcaaId": ids[1] if len(ids) >= 2 else None,
        "Location": location,
        "Attendance": attendance,
    }

    # A set table is one that contains scored-point (short_play_text) rows.
    set_tables = [t for t in doc.xpath("//table") if t.xpath('.//span[@class="short_play_text"]')]

    events: list[dict] = []

    def emit(set_no, rally, seq, touch, side, player=None, *, is_terminal=False,
             terminal_type=None, scoring_side=None, away=None, home=None):
        events.append({
            "ContestID": contest_id, "Season": season, "Set": set_no, "Rally": rally,
            "Seq": seq, "TouchType": touch, "PlayerName": player, "Side": side,
            "IsTerminal": is_terminal, "TerminalType": terminal_type,
            "ScoringSide": scoring_side, "AwayScore": away, "HomeScore": home,
            "AwayNcaaId": meta["AwayNcaaId"], "HomeNcaaId": meta["HomeNcaaId"],
            "Location": meta["Location"], "Attendance": meta["Attendance"],
        })

    for set_no, table in enumerate(set_tables, start=1):
        rally = 0
        seq = 0
        for tr in table.xpath(".//tr"):
            tds = tr.xpath("./td")
            if len(tds) != 3:
                continue
            short = tr.xpath('.//span[@class="short_play_text"]')

            # --- terminal (scored point) row: the short_play_text span sits on the scoring side ---
            if short:
                scoring = "away" if tds[0].xpath('.//span[@class="short_play_text"]') else "home"
                stext = _norm(short[0].text_content())
                ttype = _terminal_type(stext)
                # Errors are charged to the team that erred = the NON-scoring side.
                side = (("home" if scoring == "away" else "away")
                        if ttype.endswith("_error") else scoring)
                m = _SCORE_RE.match(_norm(tds[1].text_content()))
                away, home = (int(m.group(1)), int(m.group(2))) if m else (None, None)
                seq += 1
                emit(set_no, rally, seq, "terminal", side, _player(stext),
                     is_terminal=True, terminal_type=ttype, scoring_side=scoring,
                     away=away, home=home)
                continue

            # --- detail touch / sub / marker: text lives in the away or home cell ---
            away_txt, home_txt = _norm(tds[0].text_content()), _norm(tds[2].text_content())
            side = "away" if away_txt else ("home" if home_txt else None)
            text = away_txt or home_txt
            if not text or text in _SKIP_EXACT or text.startswith("Timeout") \
                    or "timeout" in text.lower() or "challenge" in text.lower():
                continue

            sub = _SUB_RE.match(text)
            if sub:
                seq += 1
                emit(set_no, rally, seq, "sub_" + sub.group(1).lower(), side, _norm(sub.group(2)))
                continue

            touch = _touch_type(text)
            if touch is None:
                continue
            if touch == "serve":
                rally += 1
                seq += 1
                emit(set_no, rally, seq, "serve", side, _player(text))
                serve_to = _SERVE_TO_RE.match(text)
                if serve_to:  # "X serves to Y" -> also a reception by the receiving side
                    seq += 1
                    emit(set_no, rally, seq, "reception",
                         "home" if side == "away" else "away", _norm(serve_to.group(2)))
            else:
                seq += 1
                emit(set_no, rally, seq, touch, side, _player(text))

    return events, meta


# --------------------------------------------------------------------------- fetch / scrape


def fetch_contest_pbp(contest_id: str, season: int) -> pd.DataFrame:
    """All touch/sub/terminal event rows for one contest (empty frame if the page has no PBP)."""
    url = f"https://stats.ncaa.org/contests/{contest_id}/play_by_play"
    html = fetch_html(url, wait_selectors=("table",))
    events, _ = parse_pbp(html, str(contest_id), season)
    if not events:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame(events, columns=COLUMNS)


def _output_path(year: int, output: Path | None) -> Path:
    if output:
        return Path(output)
    return settings.staging_dir / f"ncaa_wvb_pbp_d1_{year}.csv"


def _scrape(todo: list[str], year: int, out: Path, seen: set[str]) -> int:
    """Fetch each contest's PBP and append to the resumable CSV. Returns failure count."""
    failed = 0
    for ci, cid in enumerate(todo, 1):
        try:
            df = fetch_contest_pbp(cid, year)
        except Exception as e:
            failed += 1
            log.warning("    [%d/%d] pbp %s failed, skipping: %s", ci, len(todo), cid, e)
            continue
        if df.empty:
            log.info("    [%d/%d] pbp %s: no play-by-play", ci, len(todo), cid)
            seen.add(cid)
            continue
        _append_df(df, out)
        seen.add(cid)
        log.info("    [%d/%d] pbp %s: %d events", ci, len(todo), cid, len(df))
    return failed


def scrape_pbp(
    team_ids: Iterable[str],
    year: int,
    max_contests: int | None = None,
    output: Path | None = None,
    known_ids: set[str] | None = None,
) -> Path:
    """Scrape PBP for a full team sweep, appending to a resumable CSV (contests in CSV skipped)."""
    out = _output_path(year, output)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = _existing_contest_ids(out)
    if known_ids:
        seen |= {str(c) for c in known_ids}
    if seen:
        log.info("[resume] %d contest(s) already have PBP (CSV+DB); skipping.", len(seen))

    team_ids = [str(t) for t in team_ids]
    failed_teams = 0
    for ti, tid in enumerate(team_ids, 1):
        try:
            contests = discover_contests(tid)
        except Exception as e:
            failed_teams += 1
            log.warning("[team %d/%d] team_id=%s discover failed, skipping: %s",
                        ti, len(team_ids), tid, e)
            continue
        if max_contests:
            contests = contests[:max_contests]
        todo = [c for c in contests if c not in seen]
        log.info("[team %d/%d] team_id=%s contests=%d (todo=%d)",
                 ti, len(team_ids), tid, len(contests), len(todo))
        _scrape(todo, year, out, seen)

    fail_rate = failed_teams / max(1, len(team_ids))
    if fail_rate > settings.vb_scrape_fail_threshold:
        raise RuntimeError(
            f"pbp scrape aborted: {failed_teams}/{len(team_ids)} teams failed "
            f"({fail_rate:.0%} > {settings.vb_scrape_fail_threshold:.0%} threshold) — "
            f"likely a site-wide block or outage"
        )
    return out


def scrape_pbp_by_date(
    dates: Iterable[str],
    year: int,
    max_contests: int | None = None,
    output: Path | None = None,
    known_ids: set[str] | None = None,
) -> Path:
    """Scrape PBP only for contests played on the given ``dates`` (``MM/DD/YYYY``)."""
    out = _output_path(year, output)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = _existing_contest_ids(out)
    if known_ids:
        seen |= {str(c) for c in known_ids}
    if seen:
        log.info("[resume] %d contest(s) already have PBP (CSV+DB); skipping.", len(seen))

    dates = list(dates)
    discovered: list[str] = []
    failed_dates = 0
    for d in dates:
        try:
            ids = discover_contests_by_date(d, year)
        except Exception as e:
            failed_dates += 1
            log.warning("[scoreboard %s] discover failed, skipping: %s", d, e)
            continue
        for cid in ids:
            if cid not in discovered:
                discovered.append(cid)
        log.info("[scoreboard %s] %d contest(s)", d, len(ids))

    todo = [c for c in discovered if c not in seen]
    if max_contests:
        todo = todo[:max_contests]
    log.info("[by-date] %d date(s), %d contest(s) discovered, %d new to fetch",
             len(dates), len(discovered), len(todo))
    _scrape(todo, year, out, seen)

    if dates and failed_dates == len(dates):
        raise RuntimeError(
            f"pbp scrape aborted: all {len(dates)} scoreboard fetch(es) failed — "
            f"likely a site-wide block or outage"
        )
    return out
