"""Load authoritative per-set starters from ncaa.com (henrygd/ncaa-api) into contest_set_starters.

ncaa.com's play-by-play names each set's six rotation starters explicitly, so this replaces the
heuristic starter reconstruction from the ``pbp_events`` sub log with the real lineup — and keeps
working when stats.ncaa.org is blocked. For every contest that already carries a ``ncaa_game_id``
(resolved by ``map_ncaa_games``), we fetch the game's PBP from the sidecar, pull each set's starter
names, and upsert the rows. ncaa.com's PBP doesn't reliably tag which team each starters line belongs
to, so we assign a line to whichever of the contest's two teams its names match (six names from one
team make that unambiguous) rather than trusting the reported team — then reconcile each name to a
``players`` row by (team, season, normalized name).

Unmatched names (a spelling that doesn't line up with the roster, or a player we never rostered) are
logged and skipped rather than mis-attributed. Idempotent: each (contest, team, set) is rewritten
wholesale on every run.
"""
from __future__ import annotations

import re
import time
import unicodedata
from collections import defaultdict
from datetime import UTC, timedelta
from datetime import date as _date
from datetime import datetime as _datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..log import get_logger
from ..models import Contest, ContestSetStarter, Player
from ..scrape.ncaa_api import NcaaApiError, play_by_play
from ..util.normalize import fix_mojibake as _fix_mojibake

log = get_logger(__name__)

_POLITE_DELAY = 0.4  # seconds between per-game PBP fetches (the sidecar proxies ncaa.com upstream)


def _norm_name(name: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace — for order-sensitive matching."""
    s = unicodedata.normalize("NFKD", _fix_mojibake(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[.'`’]", "", s.lower())           # drop intra-word apostrophes/periods (O'Neil->oneil)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()   # hyphens etc. become separators (Nunez-Garcia -> two tokens)


def _name_key(name: str) -> frozenset[str]:
    """Order-insensitive token set ('Doe, Jane' and 'Jane Doe' both -> {'jane','doe'})."""
    return frozenset(_norm_name(name).split())


def _lastinit_key(name: str) -> tuple[str, str] | None:
    """(first initial, last token) — a fuzzy fallback for nicknames/dropped middles.

    'Addy Franz' and 'Addyson Franz' both -> ('a', 'franz'); 'Ava Roodbol' matches roster
    'Ava Tiessen-Roodbol' -> ('a', 'roodbol'). None for single-token names (too weak to key on).
    """
    toks = _norm_name(name).split()
    return (toks[0][0], toks[-1]) if len(toks) >= 2 else None


def _lastname_key(name: str) -> str | None:
    """Last token alone — the loosest fallback, for nicknames whose first initial also differs.

    'Arabella Dearinger' (ncaa.com) and roster 'Bella Dearinger' share only the surname. Keyed on
    surname alone it's only safe when that surname is unique on the roster, which ``_put`` enforces by
    nulling collisions. None for single-token names.
    """
    toks = _norm_name(name).split()
    return toks[-1] if len(toks) >= 2 else None


def _put(d: dict, key, pid: int) -> None:
    """Insert key->pid, but mark it None (ambiguous, never matched) if two players share the key."""
    if key is not None:
        d[key] = None if key in d and d[key] != pid else pid


class _RosterIndex:
    """A team-season roster indexed three ways for progressively fuzzier name matching.

    Exact normalized full name -> order-insensitive token set -> (first initial, last name) -> surname
    alone. Keys that two players share are nulled so an ambiguous match is skipped rather than guessed;
    the surname-only tier therefore fires only when that surname is unique on the roster.
    """
    def __init__(self, session: Session, team_id: int, season: int):
        self.by_full: dict[str, int | None] = {}
        self.by_tokens: dict[frozenset[str], int | None] = {}
        self.by_lastinit: dict[tuple[str, str], int | None] = {}
        self.by_lastname: dict[str, int | None] = {}
        self.token_sets: list[tuple[int, frozenset[str]]] = []  # (pid, tokens) for the overlap tier
        for pid, name in session.execute(
            select(Player.id, Player.name).where(Player.team_id == team_id, Player.season == season)
        ).all():
            _put(self.by_full, _norm_name(name) or None, pid)
            _put(self.by_tokens, _name_key(name) or None, pid)
            _put(self.by_lastinit, _lastinit_key(name), pid)
            _put(self.by_lastname, _lastname_key(name), pid)
            toks = _name_key(name)
            if len(toks) >= 2:
                self.token_sets.append((pid, toks))

    def _overlap_match(self, name: str) -> int | None:
        """Loosest tier: a roster player sharing >=2 name tokens, only when exactly one player does.

        Catches compound/partial names where a subset of tokens is dropped or reordered
        ('Gabriela Machin' <-> 'Gabriela Machin Borges', 'Maria Bernardita Aguilar Toranza' <->
        'Bernardita Aguilar'). Requiring two shared tokens plus uniqueness keeps it from guessing:
        if two roster players each share two tokens, we skip rather than pick.
        """
        toks = _name_key(name)
        if len(toks) < 2:
            return None
        cands = {pid for pid, ptoks in self.token_sets if len(toks & ptoks) >= 2}
        return next(iter(cands)) if len(cands) == 1 else None

    def match(self, name: str) -> int | None:
        """Resolve a name to a player id, progressively fuzzier:
        full name -> token set -> last-name+initial -> unique surname -> unique >=2-token overlap."""
        for key, table in ((_norm_name(name), self.by_full),
                           (_name_key(name), self.by_tokens),
                           (_lastinit_key(name), self.by_lastinit),
                           (_lastname_key(name), self.by_lastname)):
            pid = table.get(key) if key else None
            if pid is not None:
                return pid
        return self._overlap_match(name)


def _roster_index(session: Session, team_id: int, season: int) -> _RosterIndex:
    return _RosterIndex(session, team_id, season)


def _assign_group(player_names, sides) -> tuple[int | None, set[int], list[str]]:
    """Assign one starters line to the team whose roster its names best match.

    ncaa.com's PBP can mislabel which team a starters line belongs to, so we ignore the reported team
    and pick the contest side with the most name matches (six names come from one team, so the winner
    is unambiguous). ``sides`` is ``[(team_id, _RosterIndex), ...]``. Returns
    ``(team_id, matched_pids, missed_names)`` — ``team_id`` is None when no side matched any name, and
    ``missed_names`` are the names the winning roster still didn't resolve (all names in the no-match
    case).
    """
    best_team: int | None = None
    best: set[int] = set()
    best_missed: list[str] = list(player_names)
    for team_id, idx in sides:
        pids: set[int] = set()
        missed: list[str] = []
        for nm in player_names:
            pid = idx.match(nm)
            (pids.add(pid) if pid is not None else missed.append(nm))
        if len(pids) > len(best):
            best_team, best, best_missed = team_id, pids, missed
    return best_team, best, best_missed


def load_ncaa_lineups(
    session: Session, season: int, *,
    days_back: int | None = None, today: _date | None = None, limit: int | None = None,
) -> dict:
    """Populate ``contest_set_starters`` for the season's contests that have a ``ncaa_game_id``.

    ``days_back`` restricts to contests dated within ±days_back of ``today`` (daily incremental);
    omit for a full-season backfill. ``limit`` caps the number of games fetched (useful for a probe).
    """
    q = select(Contest).where(
        Contest.season == season, Contest.ncaa_game_id.isnot(None),
        Contest.home_team_id.isnot(None), Contest.away_team_id.isnot(None),
    )
    contests = list(session.scalars(q).all())
    if days_back is not None:
        ref = today or _datetime.now(tz=UTC).date()
        lo = (ref - timedelta(days=days_back)).isoformat()
        hi = (ref + timedelta(days=days_back)).isoformat()
        contests = [c for c in contests if c.date and lo <= c.date[:10] <= hi]
    contests.sort(key=lambda c: (c.date or ""))
    if limit is not None:
        contests = contests[:limit]

    rosters: dict[int, tuple[dict, dict]] = {}   # team_id -> (by_full, by_tokens), memoized
    games = sets_written = rows = unmatched = errors = 0
    for i, c in enumerate(contests):
        if i:
            time.sleep(_POLITE_DELAY)
        try:
            pbp = play_by_play(c.ncaa_game_id, session=None)
        except NcaaApiError as e:
            errors += 1
            log.warning("ncaa-api PBP fetch failed contest=%s game=%s: %s",
                        c.contest_id, c.ncaa_game_id, e)
            continue
        if not pbp.set_starters:
            continue
        games += 1
        # ncaa.com's PBP does NOT reliably tag which team a starters line belongs to (the group's
        # teamId/seoname can be crossed), so we IGNORE the reported team and assign each line to the
        # contest team whose roster its names actually match — six names from one team make that
        # unambiguous. Cache both rosters up front.
        for tid in (c.home_team_id, c.away_team_id):
            if tid not in rosters:
                rosters[tid] = _roster_index(session, tid, season)
        sides = [(tid, rosters[tid]) for tid in (c.home_team_id, c.away_team_id)]
        # (team_id, set_number) -> resolved player_ids, so we can rewrite each key wholesale.
        resolved: dict[tuple[int, int], set[int]] = defaultdict(set)
        for grp in pbp.set_starters:
            team_id, best, missed = _assign_group(grp.player_names, sides)
            unmatched += len(missed)
            for nm in missed:
                log.info("unmatched starter name=%r team_id=%s contest=%s set=%d",
                         nm, team_id, c.contest_id, grp.set_number)
            if team_id is not None:
                resolved[(team_id, grp.set_number)] |= best

        for (team_id, set_no), pids in resolved.items():
            session.execute(delete(ContestSetStarter).where(
                ContestSetStarter.contest_id == c.contest_id,
                ContestSetStarter.team_id == team_id,
                ContestSetStarter.set_number == set_no,
            ))
            for pid in pids:
                session.add(ContestSetStarter(
                    contest_id=c.contest_id, team_id=team_id, set_number=set_no,
                    player_id=pid, season=season,
                ))
                rows += 1
            sets_written += 1
        session.flush()

    log.info(
        "load_ncaa_lineups: %d games, %d team-sets, %d starter rows, %d unmatched, %d fetch errors "
        "(season %d)", games, sets_written, rows, unmatched, errors, season,
    )
    return {"games": games, "team_sets": sets_written, "rows": rows,
            "unmatched": unmatched, "errors": errors}
