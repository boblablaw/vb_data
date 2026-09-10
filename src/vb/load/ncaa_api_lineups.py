"""Load authoritative per-set starters from ncaa.com (henrygd/ncaa-api) into contest_set_starters.

ncaa.com's play-by-play names each set's six rotation starters explicitly, so this replaces the
heuristic starter reconstruction from the ``pbp_events`` sub log with the real lineup — and keeps
working when stats.ncaa.org is blocked. For every contest that already carries a ``ncaa_game_id``
(resolved by ``map_ncaa_games``), we fetch the game's PBP from the sidecar, pull each set's starter
names, reconcile them to our ``players`` by (team, season, normalized name), and upsert the rows.

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
from .ncaa_com_games import _slug_to_team_id

log = get_logger(__name__)

_POLITE_DELAY = 0.4  # seconds between per-game PBP fetches (the sidecar proxies ncaa.com upstream)


def _norm_name(name: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace — for order-sensitive matching."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[.'`’]", "", s.lower())           # drop intra-word apostrophes/periods (O'Neil->oneil)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()   # hyphens etc. become separators (Nunez-Garcia -> two tokens)


def _name_key(name: str) -> frozenset[str]:
    """Order-insensitive token set ('Doe, Jane' and 'Jane Doe' both -> {'jane','doe'})."""
    return frozenset(_norm_name(name).split())


def _roster_index(session: Session, team_id: int, season: int) -> tuple[dict, dict]:
    """Two lookups for a team's roster: normalized full-name -> pid, and token-set -> pid.

    Keys that collide across two players are dropped (value None) so an ambiguous match is skipped
    rather than guessed.
    """
    by_full: dict[str, int | None] = {}
    by_tokens: dict[frozenset[str], int | None] = {}
    for pid, name in session.execute(
        select(Player.id, Player.name).where(Player.team_id == team_id, Player.season == season)
    ).all():
        full = _norm_name(name)
        if full:
            by_full[full] = None if full in by_full and by_full[full] != pid else pid
        tok = _name_key(name)
        if tok:
            by_tokens[tok] = None if tok in by_tokens and by_tokens[tok] != pid else pid
    return by_full, by_tokens


def _match(name: str, by_full: dict, by_tokens: dict) -> int | None:
    """Resolve a starter name to a player id: exact normalized string first, then token set."""
    pid = by_full.get(_norm_name(name))
    if pid is not None:
        return pid
    return by_tokens.get(_name_key(name))


def load_ncaa_lineups(
    session: Session, season: int, *,
    days_back: int | None = None, today: _date | None = None, limit: int | None = None,
) -> dict:
    """Populate ``contest_set_starters`` for the season's contests that have a ``ncaa_game_id``.

    ``days_back`` restricts to contests dated within ±days_back of ``today`` (daily incremental);
    omit for a full-season backfill. ``limit`` caps the number of games fetched (useful for a probe).
    """
    slug_map = _slug_to_team_id(session)

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
        valid_team_ids = {c.home_team_id, c.away_team_id}
        # (team_id, set_number) -> resolved player_ids, so we can rewrite each key wholesale.
        resolved: dict[tuple[int, int], set[int]] = defaultdict(set)
        for grp in pbp.set_starters:
            team_id = slug_map.get(grp.seoname)
            if team_id not in valid_team_ids:
                # A starters line for a team we couldn't map to this contest — skip defensively.
                continue
            if team_id not in rosters:
                rosters[team_id] = _roster_index(session, team_id, season)
            by_full, by_tokens = rosters[team_id]
            for nm in grp.player_names:
                pid = _match(nm, by_full, by_tokens)
                if pid is None:
                    unmatched += 1
                    log.info("unmatched starter name=%r team_id=%s contest=%s set=%d",
                             nm, team_id, c.contest_id, grp.set_number)
                    continue
                resolved[(team_id, grp.set_number)].add(pid)

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
