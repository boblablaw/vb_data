"""Per-game (contest) per-player stats from stats.ncaa.org — the PRIMARY stat source.

Discovery has two modes:
  * full team sweep — discover_contests(team_id): read each team page for its
    /contests/<id>/box_score links (one page per team; the weekly reconcile).
  * date-targeted — discover_contests_by_date(game_date, year): read the daily scoreboard
    for exactly the contests played that day (one fetch per date; the daily fast path).

Either way, fetch_contest_individual_stats(contest_id) reads /contests/<id>/individual_stats,
which carries a per-player stat table for each of the two teams.

Writes a raw CSV (exports/ncaa_wvb_game_stats_d1_<year>.csv). The scrape is resumable:
contests already present in the CSV (or the ``known_ids`` seed) are skipped.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from io import StringIO
from pathlib import Path

import pandas as pd

from ..config import settings
from ..fetch import fetch_html
from ..log import get_logger
from ..util import parse_ncaa_datetime

log = get_logger(__name__)

CONTEST_RE = re.compile(r"/contests/(\d+)/box_score")
PLAYER_LINK_RE = re.compile(r'/players/(\d+)"[^>]*>\s*([^<]+?)\s*<')
TEAM_LINK_RE = re.compile(r'/teams/(\d+)')
# The two stat tables (and scoreboard) list the visiting team first, home team second.
SIDE_LABELS = ("Away", "Home")


def discover_contests(team_id: str) -> list[str]:
    """Unique contest ids (page order) for a team's season."""
    html = fetch_html(f"https://stats.ncaa.org/teams/{team_id}", wait_selectors=("table",))
    out: list[str] = []
    for cid in CONTEST_RE.findall(html):
        if cid not in out:
            out.append(cid)
    return out


def discover_contests_by_date(game_date: str, year: int) -> list[str]:
    """Unique contest ids played on ``game_date`` (``MM/DD/YYYY``), via the daily scoreboard.

    Far cheaper than sweeping every team page: one fetch returns exactly the contests played
    that day. ``academic_year`` is ``year + 1`` (NCAA labels academic years by their ending
    year), matching the team-list / roster URL convention.
    """
    url = (
        "https://stats.ncaa.org/contests/livestream_scoreboards"
        f"?utf8=%E2%9C%93&sport_code=WVB&academic_year={year + 1}&division=1"
        f"&game_date={game_date}&conf_id=-1&tournament_id=&commit=Submit"
    )
    html = fetch_html(url, wait_selectors=("table",))
    out: list[str] = []
    for cid in CONTEST_RE.findall(html):
        if cid not in out:
            out.append(cid)
    return out


def _player_id_map(html: str) -> dict[str, str]:
    return {name.strip(): pid for pid, name in PLAYER_LINK_RE.findall(html)}


def _int_or_none(v: object) -> int | None:
    """Parse a linescore cell to int; ``None`` for blanks/NaN/non-numeric."""
    try:
        return int(float(str(v)))
    except (ValueError, TypeError):
        return None


def _read_tables(html: str) -> list[pd.DataFrame]:
    """``pd.read_html`` but returns ``[]`` instead of raising on a table-less page.

    An unplayed/future contest's individual_stats page has no stat tables, and pandas raises
    ``ValueError("No tables found")`` in that case — which would otherwise abort a whole backfill
    on the first such contest. Treat "no tables" as an empty result.
    """
    try:
        return pd.read_html(StringIO(html))
    except ValueError:
        return []


def _parse_linescore(html: str) -> dict | None:
    """Match linescore from an individual_stats page, or ``None`` if absent.

    Returns ``{"away_sets_won", "home_sets_won", "away_points", "home_points"}`` where the
    ``*_points`` lists are per-set point totals (played sets only). The linescore table's last
    column ("S") is sets won and the columns before it (headed 1..5) are per-set points; the
    visiting team is the first data row and the home team the second — the same away-first /
    home-second order the two team links follow. ``None`` when no linescore is present (e.g.
    an unplayed contest or a malformed page).
    """
    for table in _read_tables(html):
        vals = table.astype(str)
        nrows = vals.shape[0]
        for ri in range(nrows):
            row = list(vals.iloc[ri])
            if "S" not in row:
                continue
            s_col = row.index("S")
            # A real linescore header has set numbers (1..5) before the "S" (sets-won) column.
            set_cols = [c for c in range(s_col) if row[c] in {"1", "2", "3", "4", "5"}]
            if not set_cols or ri + 2 >= nrows:
                continue
            away_sets = _int_or_none(vals.iloc[ri + 1, s_col])
            home_sets = _int_or_none(vals.iloc[ri + 2, s_col])
            if away_sets is None or home_sets is None:
                continue
            away_pts = [_int_or_none(vals.iloc[ri + 1, c]) for c in set_cols]
            home_pts = [_int_or_none(vals.iloc[ri + 2, c]) for c in set_cols]
            # Keep only sets both teams actually played (trailing blanks are unplayed sets).
            played = [
                (a, h) for a, h in zip(away_pts, home_pts) if a is not None and h is not None
            ]
            return {
                "away_sets_won": away_sets,
                "home_sets_won": home_sets,
                "away_points": [a for a, _ in played],
                "home_points": [h for _, h in played],
            }
    return None


def contest_meta(html: str) -> dict[str, object]:
    """Contest date, home/away NCAA team ids, set-win totals, and per-set scores.

    The scoreboard lists the visiting team first and the home team second; the two team
    links appear in that document order, so the first distinct id is away, the second home.
    The linescore's "S" column gives each side's sets won (winner = the side with more), and
    the per-set columns give each set's point totals (stored as ``SetScores`` JSON).
    """
    ids: list[str] = []
    for tid in TEAM_LINK_RE.findall(html):
        if tid not in ids:
            ids.append(tid)
    ls = _parse_linescore(html)
    set_scores = (
        {"away": ls["away_points"], "home": ls["home_points"]}
        if ls and (ls["away_points"] or ls["home_points"])
        else None
    )
    return {
        "Date": parse_ncaa_datetime(html),
        "AwayTeamNcaaId": ids[0] if len(ids) >= 1 else None,
        "HomeTeamNcaaId": ids[1] if len(ids) >= 2 else None,
        "AwaySetsWon": ls["away_sets_won"] if ls else None,
        "HomeSetsWon": ls["home_sets_won"] if ls else None,
        "SetScores": set_scores,
    }


def fetch_contest_individual_stats(contest_id: str) -> pd.DataFrame:
    """Per-player stat rows for both teams in a contest (empty if none)."""
    url = f"https://stats.ncaa.org/contests/{contest_id}/individual_stats"
    html = fetch_html(url, wait_selectors=("table",))
    id_map = _player_id_map(html)
    meta = contest_meta(html)
    frames: list[pd.DataFrame] = []
    stat_idx = 0  # index among the qualifying stat tables: 0 -> Away, 1 -> Home
    for table in _read_tables(html):
        cols = [str(c) for c in table.columns]
        if "Name" not in cols or "Kills" not in cols:
            continue
        t = table.copy()
        t = t[t["Name"].notna()]
        side = SIDE_LABELS[stat_idx] if stat_idx < len(SIDE_LABELS) else str(stat_idx)
        stat_idx += 1
        t.insert(0, "ContestID", str(contest_id))
        t.insert(1, "TeamSide", side)
        t.insert(2, "PlayerID", t["Name"].astype(str).str.strip().map(id_map))
        t.insert(3, "Date", meta["Date"])
        t.insert(4, "AwayTeamNcaaId", meta["AwayTeamNcaaId"])
        t.insert(5, "HomeTeamNcaaId", meta["HomeTeamNcaaId"])
        t.insert(6, "AwaySetsWon", meta["AwaySetsWon"])
        t.insert(7, "HomeSetsWon", meta["HomeSetsWon"])
        t.insert(8, "SetScores", json.dumps(meta["SetScores"]) if meta["SetScores"] else "")
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


def _frame_contest(cid: str, year: int, team_id: str = "") -> pd.DataFrame:
    """Fetch one contest's stats and prepend the ``TeamID``/``Season`` ledger columns.

    ``team_id`` is the discovering team in a full sweep; it's left empty in date mode (the
    loader resolves each row's team from the player's roster, not this column). Returns an
    empty frame for a contest with no stats.
    """
    df = fetch_contest_individual_stats(cid)
    if df.empty:
        return df
    df.insert(0, "TeamID", str(team_id))
    df.insert(1, "Season", year)
    return df


def _append_df(df: pd.DataFrame, out: Path) -> None:
    """Append rows to the resumable CSV, failing loud on schema drift.

    Adding stat columns to the scraper without rewriting the existing on-disk header once
    misaligned every appended row (27-col header, 30-col rows) and silently broke the loader
    for days. Guard it: if the on-disk header no longer matches the frame's columns, raise
    instead of appending garbage. Migrate/rebuild the CSV, then re-run.
    """
    if out.exists():
        existing = list(pd.read_csv(out, nrows=0).columns)
        if existing != list(df.columns):
            raise RuntimeError(
                f"game-stats CSV schema drift in {out.name}: on-disk header has "
                f"{len(existing)} columns {existing}, new rows have {len(df.columns)} "
                f"columns {list(df.columns)}. Migrate/rebuild the CSV before appending "
                f"(see deploy/OCI_SETUP.md)."
            )
        df.to_csv(out, mode="a", header=False, index=False)
    else:
        df.to_csv(out, mode="w", header=True, index=False)


def scrape_game_stats(
    team_ids: Iterable[str],
    year: int,
    max_contests: int | None = None,
    output: Path | None = None,
    known_ids: set[str] | None = None,
) -> Path:
    """Scrape per-game stats for the given team ids, appending to a resumable CSV.

    ``known_ids`` (e.g. contests already in the DB) are treated as already-fetched in addition
    to whatever is in the CSV, so the "add only" guarantee survives even if the CSV is cleared.
    """
    out = _output_path(year, output)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = _existing_contest_ids(out)
    if known_ids:
        seen |= {str(c) for c in known_ids}
    if seen:
        log.info("[resume] %d contest(s) already known (CSV+DB); skipping.", len(seen))

    team_ids = [str(t) for t in team_ids]
    failed_teams = 0
    failed_contests = 0
    for ti, tid in enumerate(team_ids, 1):
        try:
            contests = discover_contests(tid)
        except Exception as e:
            # A flaky team page must not abort the whole sweep. Skip it (not added to `seen`,
            # so the next run retries it) and keep going; the systemic-failure gate below
            # still trips if too many teams fail.
            failed_teams += 1
            log.warning("[team %d/%d] team_id=%s discover failed, skipping: %s",
                        ti, len(team_ids), tid, e)
            continue
        if max_contests:
            contests = contests[:max_contests]
        todo = [c for c in contests if c not in seen]
        log.info("[team %d/%d] team_id=%s contests=%d (todo=%d)",
                 ti, len(team_ids), tid, len(contests), len(todo))
        for ci, cid in enumerate(todo, 1):
            try:
                df = _frame_contest(cid, year, tid)
            except Exception as e:
                # Skip this one contest; do NOT add to `seen` so it's retried next run.
                failed_contests += 1
                log.warning("    [%d/%d] contest %s failed, skipping: %s", ci, len(todo), cid, e)
                continue
            if df.empty:
                log.info("    [%d/%d] contest %s: no stats", ci, len(todo), cid)
                seen.add(cid)
                continue
            _append_df(df, out)
            seen.add(cid)
            log.info("    [%d/%d] contest %s: %d rows", ci, len(todo), cid, len(df))

    log.info("[done] teams=%d failed_teams=%d failed_contests=%d",
             len(team_ids), failed_teams, failed_contests)
    fail_rate = failed_teams / max(1, len(team_ids))
    if fail_rate > settings.vb_scrape_fail_threshold:
        raise RuntimeError(
            f"scrape aborted: {failed_teams}/{len(team_ids)} teams failed "
            f"({fail_rate:.0%} > {settings.vb_scrape_fail_threshold:.0%} threshold) — "
            f"likely a site-wide block or outage"
        )
    return out


def scrape_game_stats_by_date(
    dates: Iterable[str],
    year: int,
    max_contests: int | None = None,
    output: Path | None = None,
    known_ids: set[str] | None = None,
) -> Path:
    """Scrape only the contests played on the given ``dates`` (``MM/DD/YYYY``).

    Discovery is one daily-scoreboard fetch per date instead of a page per team — the
    daily-job fast path. Resumable CSV and ``known_ids`` semantics match the full sweep, so
    the two modes append to the same file interchangeably.
    """
    out = _output_path(year, output)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = _existing_contest_ids(out)
    if known_ids:
        seen |= {str(c) for c in known_ids}
    if seen:
        log.info("[resume] %d contest(s) already known (CSV+DB); skipping.", len(seen))

    dates = list(dates)
    discovered: list[str] = []
    failed_dates = 0
    for d in dates:
        try:
            ids = discover_contests_by_date(d, year)
        except Exception as e:
            # A flaky scoreboard fetch must not abort the run; other dates still proceed.
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

    failed_contests = 0
    for ci, cid in enumerate(todo, 1):
        try:
            df = _frame_contest(cid, year)
        except Exception as e:
            failed_contests += 1
            log.warning("    [%d/%d] contest %s failed, skipping: %s", ci, len(todo), cid, e)
            continue
        if df.empty:
            log.info("    [%d/%d] contest %s: no stats", ci, len(todo), cid)
            seen.add(cid)
            continue
        _append_df(df, out)
        seen.add(cid)
        log.info("    [%d/%d] contest %s: %d rows", ci, len(todo), cid, len(df))

    log.info("[by-date] done: dates=%d failed_dates=%d contests=%d failed_contests=%d",
             len(dates), failed_dates, len(todo), failed_contests)
    # If every date's scoreboard fetch failed, that's site-wide — surface it (mirrors the
    # team-sweep fail-threshold gate) rather than silently reporting "0 new".
    if dates and failed_dates == len(dates):
        raise RuntimeError(
            f"scrape aborted: all {len(dates)} scoreboard fetch(es) failed — "
            f"likely a site-wide block or outage"
        )
    return out
