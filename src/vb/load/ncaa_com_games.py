"""Resolve each game's ncaa.com id and store it on ``contests``/``schedule``.

ncaa.com uses a different id system from our stats.ncaa.org ``contest_id``, so a public-site link
can't be built from the ids we already have. This pulls ncaa.com's own scoreboard (one HTTP call
per date) and matches each ncaa.com game to our rows on (date + unordered team pair) — teams bridged
by slugging our ``short_name`` to ncaa.com's ``seoname`` — then writes the recovered ncaa.com id.

Idempotent: only writes when the id is new/changed. Safe to run repeatedly (daily incremental via
``days_back``; weekly full-season backfill with no window).
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import UTC, timedelta
from datetime import date as _date
from datetime import datetime as _datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..log import get_logger
from ..models import Contest, Schedule, Team
from ..scrape.ncaa_com_games import fetch_games
from ..util import slug_school

log = get_logger(__name__)

_POLITE_DELAY = 0.4  # seconds between per-date fetches


def _slug_to_team_id(session: Session) -> dict[str, int]:
    """ncaa.com-style slug -> team_id, from each team's short_name / name / aliases."""
    out: dict[str, int] = {}
    for t in session.scalars(select(Team)).all():
        for cand in (t.short_name, t.name, *(t.aliases or [])):
            key = slug_school(cand) if cand else ""
            if key:
                out.setdefault(key, t.id)
    return out


def _resolve_pair(game, slug_map: dict[str, int]) -> frozenset[int] | None:
    """Map a ncaa.com game's two teams to our team_ids (seoname first, name_short fallback)."""
    ids: set[int] = set()
    for seo, ns in zip(game.seonames, game.name_shorts):
        tid = slug_map.get(seo) or slug_map.get(slug_school(ns))
        if tid:
            ids.add(tid)
    return frozenset(ids) if len(ids) == 2 else None


def map_ncaa_games(
    session: Session, season: int, *, days_back: int | None = None, today: _date | None = None
) -> dict:
    """Backfill ``ncaa_game_id`` for the season. With ``days_back`` set, only queries dates within
    ±days_back of ``today`` (recent results + near-term upcoming); otherwise the whole season."""
    slug_map = _slug_to_team_id(session)

    contests_by_key: dict[tuple, Contest] = {}
    for c in session.scalars(select(Contest).where(Contest.season == season)).all():
        if c.home_team_id and c.away_team_id:
            key = ((c.date or "")[:10], frozenset({c.home_team_id, c.away_team_id}))
            contests_by_key[key] = c

    sched_by_key: dict[tuple, list[Schedule]] = defaultdict(list)
    for s in session.scalars(
        select(Schedule).where(Schedule.season == season, Schedule.opponent_team_id.isnot(None))
    ).all():
        sched_by_key[(s.date, frozenset({s.team_id, s.opponent_team_id}))].append(s)

    # Dates worth querying = the days our own data has games on (bounds the fetch count).
    all_dates = {k[0] for k in contests_by_key if k[0]} | {k[0] for k in sched_by_key if k[0]}
    dates = sorted(all_dates)
    if days_back is not None:
        ref = today or _datetime.now(tz=UTC).date()
        lo, hi = (ref - timedelta(days=days_back)).isoformat(), (ref + timedelta(days=days_back)).isoformat()
        dates = [d for d in dates if lo <= d <= hi]

    updated = matched = unresolved = 0
    for i, d in enumerate(dates):
        try:
            day = _date.fromisoformat(d)
        except ValueError:
            continue
        if i:
            time.sleep(_POLITE_DELAY)
        for game in fetch_games(day, season):
            pair = _resolve_pair(game, slug_map)
            if pair is None:
                unresolved += 1
                continue
            key = (game.date, pair)
            hit = False
            c = contests_by_key.get(key)
            if c is not None:
                hit = True
                if c.ncaa_game_id != game.ncaa_game_id:
                    c.ncaa_game_id = game.ncaa_game_id
                    updated += 1
            for s in sched_by_key.get(key, ()):  # both per-team perspectives
                hit = True
                if s.ncaa_game_id != game.ncaa_game_id:
                    s.ncaa_game_id = game.ncaa_game_id
                    updated += 1
            if hit:
                matched += 1

    session.flush()
    log.info(
        "map_ncaa_games: %d dates, %d games matched, %d rows updated, %d unresolved (season %d)",
        len(dates), matched, updated, unresolved, season,
    )
    return {"dates": len(dates), "matched": matched, "updated": updated, "unresolved": unresolved}
