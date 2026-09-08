"""Match feed broadcast listings to our games and upsert the ``broadcasts`` table.

Mirrors :func:`vb.load.ncaa_com_games.map_ncaa_games`: resolve each feed listing's two team strings
to our team ids, then attach on **(date + unordered team pair)** with a ±1-day tolerance (Hawaii /
Pacific date drift). The raw network string is canonicalized via :mod:`vb.scrape.networks`.

Sources are processed **ICS first, then TPS playlist, then TPS EPG**; a (date, pair, network) is
written once, so the primary ICS feed wins and TPS only *adds* networks a game didn't already have.
Live airings beat replays for the same key. The refreshed date window is cleared before insert so a
game's TV assignment can change or disappear day-to-day without leaving stale tags.

Idempotent; safe to re-run. TPS feeds are skipped entirely when creds are absent (``settings``).
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, timedelta
from datetime import date as _date
from datetime import datetime as _datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..log import get_logger
from ..models import Broadcast, Contest, Schedule, Team
from ..scrape import networks
from ..scrape.broadcasts import (
    FeedBroadcast,
    fetch_epg,
    fetch_ics,
    fetch_playlist,
)
from ..util import normalize_school_key, slug_school

log = get_logger(__name__)

_RANK_PREFIX_RE = re.compile(r"^\s*(?:#|No\.?\s*|\(\s*)\d+\)?\s*", re.IGNORECASE)

# ESPN flavor refinement (see ingest_broadcasts): the generic ICS label yields to any specific one.
_ESPN_GENERIC = "ESPN/ESPN+"
_ESPN_SPECIFIC = {"ESPN", "ESPN2", "ESPNU", "ESPN+", "ESPNews"}
# Conference streaming overflow that is delivered *through* ESPN+ (the ESPN app), so an ICS opaque
# espn.com watch link that resolves to the generic label is really pointing at one of these — the
# "+" arm of "ESPN/ESPN+" *is* this stream. When such a feed names it for the same game, the generic
# is the same broadcast under a vaguer name, so it yields exactly like a specific ESPN flavor does.
# (Kept to the ESPN+-carried *extras*; linear ACCN/SECN are their own channels, not ESPN/ESPN+.)
_ESPN_PLUS_CARRIED = {"ACC Network Extra", "SEC Network+"}


def _name_to_team_id(session: Session) -> dict[str, int]:
    """Feed-name -> team_id, keyed by both slug and stop-word-folded forms of every known name."""
    out: dict[str, int] = {}
    for t in session.scalars(select(Team)).all():
        for cand in (t.short_name, t.name, *(t.aliases or [])):
            if not cand:
                continue
            for key in (slug_school(cand), normalize_school_key(cand)):
                if key:
                    out.setdefault(key, t.id)
    return out


def _resolve_team(name: str | None, name_map: dict[str, int]) -> int | None:
    if not name:
        return None
    cleaned = _RANK_PREFIX_RE.sub("", name).strip()
    for key in (slug_school(cleaned), normalize_school_key(cleaned)):
        tid = name_map.get(key)
        if tid:
            return tid
    return None


def _collect_feeds() -> list[FeedBroadcast]:
    """Fetch every source, in precedence order (ICS primary, then TPS fallback)."""
    feeds = list(fetch_ics())
    if settings.tps_enabled:
        feeds.extend(fetch_playlist(settings.tps_playlist_url))
        feeds.extend(fetch_epg(settings.tps_epg_url))
    else:
        log.info("TPS creds absent; skipping playlist + EPG (ICS-only run)")
    return feeds


def ingest_broadcasts(
    session: Session,
    season: int,
    *,
    days_back: int = 3,
    days_ahead: int = 10,
    today: _date | None = None,
    feeds: list[FeedBroadcast] | None = None,
) -> dict:
    """Refresh ``broadcasts`` for games within [today-days_back, today+days_ahead].

    ``feeds`` may be supplied directly (tests); otherwise every source is fetched live.
    """
    ref = today or _datetime.now(tz=UTC).date()
    lo, hi = (ref - timedelta(days=days_back)).isoformat(), (ref + timedelta(days=days_ahead)).isoformat()

    name_map = _name_to_team_id(session)

    # Our games keyed by (date, unordered pair) — contests (played) + schedule stubs (upcoming).
    games_by_key: dict[tuple[str, frozenset[int]], bool] = {}   # value: is_contest
    pair_dates: dict[frozenset[int], set[str]] = defaultdict(set)
    for c in session.scalars(select(Contest).where(Contest.season == season)).all():
        if c.home_team_id and c.away_team_id and c.date:
            d = c.date[:10]
            if lo <= d <= hi:
                pair = frozenset({c.home_team_id, c.away_team_id})
                games_by_key[(d, pair)] = True
                pair_dates[pair].add(d)
    for s in session.scalars(
        select(Schedule).where(Schedule.season == season, Schedule.opponent_team_id.isnot(None))
    ).all():
        if s.date and lo <= s.date <= hi:
            pair = frozenset({s.team_id, s.opponent_team_id})
            games_by_key.setdefault((s.date, pair), False)
            pair_dates[pair].add(s.date)

    feeds = feeds if feeds is not None else _collect_feeds()

    # Accumulate one row per (date, ordered pair, network); first writer (ICS, then live) wins.
    rows: dict[tuple[str, int, int, str], dict] = {}
    matched = unresolved = 0
    for fb in feeds:
        a = _resolve_team(fb.team_a, name_map)
        b = _resolve_team(fb.team_b, name_map)
        if not a or not b or a == b:
            unresolved += 1
            continue
        pair = frozenset({a, b})
        norm = networks.normalize(fb.network_raw)
        if norm is None:
            continue                                    # unknown/dropped (e.g. Disney+)
        label, logo_key = norm

        # Which of our game-dates does this listing attach to? ±1 day when the feed gave a date;
        # otherwise the feed listing floats, so attach only if the pair plays exactly once in-window.
        target_dates: list[str] = []
        if fb.date:
            for delta in (0, -1, 1):
                try:
                    cand = (_date.fromisoformat(fb.date) + timedelta(days=delta)).isoformat()
                except ValueError:
                    continue
                if (cand, pair) in games_by_key:
                    target_dates.append(cand)
        else:
            in_window = sorted(pair_dates.get(pair, ()))
            if len(in_window) == 1:
                target_dates = in_window
            elif fb.is_live:
                # A dateless *live* slot (a TPS "on now" channel) is airing today, so pin it to the
                # pair's meeting within a day of now — that resolves it even when the two teams play
                # more than once in-window (a floating dateless slot otherwise can't disambiguate,
                # and would be dropped). Only when exactly one meeting is near today, to stay safe.
                near = [d for d in in_window
                        if abs((_date.fromisoformat(d) - ref).days) <= 1]
                if len(near) == 1:
                    target_dates = near
        if not target_dates:
            unresolved += 1
            continue
        matched += 1

        team_a_id, team_b_id = sorted((a, b))
        for d in target_dates:
            key = (d, team_a_id, team_b_id, label)
            existing = rows.get(key)
            if existing is None:
                rows[key] = {
                    "season": season, "game_date": d,
                    "team_a_id": team_a_id, "team_b_id": team_b_id,
                    "network": label, "logo_key": logo_key, "source": fb.source,
                    "is_live": fb.is_live, "raw_channel": fb.network_raw,
                    "start_utc": fb.start_utc, "channel_no": fb.channel_no,
                }
            else:
                if fb.is_live and not existing["is_live"]:
                    existing["is_live"] = True            # live airing beats an earlier replay row
                    existing["start_utc"] = fb.start_utc or existing["start_utc"]
                if not existing.get("channel_no") and fb.channel_no:
                    existing["channel_no"] = fb.channel_no   # enrich ICS-primary row with TPS slot

    # ESPN refinement: "ESPN/ESPN+" is the honest fallback for the ICS opaque espn.com watch link
    # (flavor unknowable). If any source (typically TPS) names a specific ESPN-family channel — or an
    # ESPN+-carried conference extra (ACCNX / SECN+), which the generic's "+" arm literally is — for
    # the same game, drop the generic so the card shows just the real feed instead of both.
    by_game: dict[tuple[str, int, int], set[str]] = defaultdict(set)
    for (d, a_id, b_id, label) in rows:
        by_game[(d, a_id, b_id)].add(label)
    for (d, a_id, b_id), labels in by_game.items():
        if _ESPN_GENERIC in labels and (labels & (_ESPN_SPECIFIC | _ESPN_PLUS_CARRIED)):
            rows.pop((d, a_id, b_id, _ESPN_GENERIC), None)

    # Freeze history, refresh forward: only clear TODAY-and-future, then re-insert (a game's TV
    # assignment can still change up to game time). Past games are never re-deleted — the feeds drop
    # them within a couple of days, so a re-delete there would lose a found tag before it froze.
    # on_conflict_do_nothing preserves already-frozen past rows that are still matched from the feeds
    # (e.g. yesterday's game still in the ICS window) while the just-cleared future rows insert fresh.
    today_iso = ref.isoformat()
    session.execute(
        delete(Broadcast).where(
            Broadcast.season == season, Broadcast.game_date >= today_iso, Broadcast.game_date <= hi
        )
    )
    session.flush()
    if rows:
        session.execute(
            pg_insert(Broadcast)
            .values(list(rows.values()))
            .on_conflict_do_nothing(constraint="uq_broadcast")
        )
    session.flush()

    log.info(
        "ingest_broadcasts: %d feeds, %d matched, %d rows upserted, %d unresolved (season %d, %s..%s)",
        len(feeds), matched, len(rows), unresolved, season, lo, hi,
    )
    return {"feeds": len(feeds), "matched": matched, "upserted": len(rows), "unresolved": unresolved}
