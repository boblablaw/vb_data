"""League-wide scoreboard: played contests + upcoming scheduled games for a date/range/week."""
from __future__ import annotations

from collections import defaultdict
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...log import get_logger
from ...models import Broadcast, Contest, ContestWeek, Schedule
from ...scrape import ncaa_api
from ...util import slug_school
from ..deps import get_session
from ..schemas import BroadcastTag, ScoreboardGame
from .contests import _team_refs

log = get_logger(__name__)

router = APIRouter(prefix="/games", tags=["games"])

_ET = ZoneInfo("America/New_York")

# The scoreboard is user-independent and slow-changing (scores trickle in over minutes, not
# seconds), so let the browser serve a fresh copy instantly and revalidate in the background.
SCOREBOARD_CACHE_CONTROL = "public, max-age=120, stale-while-revalidate=600"
# When the board carries in-progress / just-finished games, shorten the browser cache so live
# scores stay current (the frontend also polls every ~60s).
SCOREBOARD_CACHE_CONTROL_LIVE = "public, max-age=30, stale-while-revalidate=60"


def _side_slug(team, name: str | None) -> str:
    """ncaa.com-style slug for one side of a game, from its Team ref (preferred) or bare name."""
    if team is not None:
        return slug_school(team.short_name or team.name or "")
    return slug_school(name) if name else ""


def _merge_live_board(games: list[ScoreboardGame], start: str, end_excl: str) -> bool:
    """Overlay in-progress / just-finished ncaa.com scores from the henrygd sidecar onto the
    ``upcoming`` stubs in ``games`` (an authoritative played ``Contest`` is never touched).

    Best-effort and additive: any sidecar failure leaves the board exactly as it was. Returns True
    if any game ended up ``live``/``final_pending`` so the caller can shorten the browser cache.
    """
    # Only today and yesterday (ET) can carry a game worth overlaying — anything older is already
    # covered by the authoritative box-score scrape. Skip entirely if neither is in the window.
    today_et = _datetime.now(tz=_ET).date()
    live_days = [
        d for d in (today_et, today_et - timedelta(days=1)) if start <= d.isoformat() < end_excl
    ]
    if not live_days:
        return False
    upcoming = [g for g in games if g.status == "upcoming"]
    if not upcoming:
        return False

    by_id = {g.ncaa_game_id: g for g in upcoming if g.ncaa_game_id}
    by_pair: dict[tuple, ScoreboardGame] = {}
    for g in upcoming:
        slugs = frozenset(
            s for s in (_side_slug(g.home_team, g.home_name), _side_slug(g.away_team, g.away_name))
            if s
        )
        if len(slugs) == 2:
            by_pair[((g.date or "")[:10], slugs)] = g

    any_live = False
    for day in live_days:
        try:
            board = ncaa_api.scoreboard_cached(day)
        except Exception as e:  # sidecar down/slow/blocked — degrade to the plain board
            log.warning("live scoreboard merge skipped for %s: %s", day, e)
            continue
        for ag in board:
            state = (ag.game_state or "").lower()
            if state not in ("live", "final"):
                continue  # 'pre' games are already the right 'upcoming' card
            g = by_id.get(ag.ncaa_game_id)
            if g is None:  # not yet mapped to a ncaa id — fall back to date + team pair
                slugs = frozenset(s for s in ag.seonames if s)
                if len(slugs) == 2:
                    g = by_pair.get((ag.date, slugs))
            if g is None or g.status not in ("upcoming", "live", "final_pending"):
                continue
            # ncaa.com lists seonames (away, home); orient its home/away sets onto our slots, which
            # may differ (neutral-site games especially). Match our home side to a ncaa side by slug.
            ncaa_away_seo = (ag.seonames[0] if ag.seonames else "")
            home_slug = _side_slug(g.home_team, g.home_name)
            if home_slug and home_slug == ncaa_away_seo:
                g.home_sets_won, g.away_sets_won = ag.away_sets_won, ag.home_sets_won  # flipped
            else:
                g.home_sets_won, g.away_sets_won = ag.home_sets_won, ag.away_sets_won  # aligned
            if state == "live":
                g.status = "live"
                g.live_period = ag.current_period
            else:  # final on ncaa.com, but the authoritative box score hasn't been scraped yet
                g.status = "final_pending"
                g.live_period = None
            any_live = True
    return any_live


@router.get("", response_model=list[ScoreboardGame])
def scoreboard(
    response: Response,
    season: int = Query(...),
    date: str | None = Query(None, description="single day, YYYY-MM-DD"),
    start: str | None = Query(None, description="range start (inclusive)"),
    end: str | None = Query(None, description="range end (inclusive)"),
    week: int | None = Query(None, description="season week number"),
    db: Session = Depends(get_session),
):
    """Games in a date window. Played contests are authoritative; the two per-team ``schedule``
    perspectives of an upcoming game are deduped into one row. Pass ``date``, ``start``+``end``,
    or ``week`` (resolved to that week's Mon–Sun span)."""
    response.headers["Cache-Control"] = SCOREBOARD_CACHE_CONTROL
    # ``contests.date`` carries a time suffix (e.g. "2026-09-07 20:00"), so an inclusive upper
    # bound of the last day (``<= "2026-09-07"``) would drop that day's games. Use an exclusive
    # upper bound one day past ``end`` instead.
    if week is not None:
        monday = db.scalar(
            select(ContestWeek.week_monday)
            .where(ContestWeek.season == season, ContestWeek.week_number == week)
            .limit(1)
        )
        if monday is None:
            return []
        start = monday.isoformat()
        end_excl = (monday + timedelta(days=7)).isoformat()
    elif date:
        start = date
        end_excl = (_date.fromisoformat(date) + timedelta(days=1)).isoformat()
    elif start and end:
        end_excl = (_date.fromisoformat(end) + timedelta(days=1)).isoformat()
    else:
        raise HTTPException(400, "provide date, start+end, or week")

    # Widen the contest lookup by a day on each side (for DEDUP ONLY — out-of-window contests are
    # not emitted). A schedule stub and its played contest sometimes disagree on the calendar day:
    # late games in Hawaii/Pacific get their ``contests.date`` stored a day ahead of the schedule.
    lookup_start = (_date.fromisoformat(start) - timedelta(days=1)).isoformat()
    lookup_end_excl = (_date.fromisoformat(end_excl) + timedelta(days=1)).isoformat()
    contests = db.scalars(
        select(Contest).where(
            Contest.season == season, Contest.date >= lookup_start, Contest.date < lookup_end_excl
        )
    ).all()
    weeks = dict(
        db.execute(
            select(ContestWeek.contest_id, ContestWeek.week_number)
            .where(ContestWeek.contest_id.in_([c.contest_id for c in contests]))
        ).all()
    ) if contests else {}
    sched = db.scalars(
        select(Schedule).where(
            Schedule.season == season, Schedule.result_raw.is_(None),
            Schedule.date >= start, Schedule.date < end_excl,
        )
    ).all()

    ids: set[int] = set()
    for c in contests:
        ids.update(x for x in (c.home_team_id, c.away_team_id) if x)
    for s in sched:
        ids.update(x for x in (s.team_id, s.opponent_team_id) if x)
    refs = _team_refs(db, *ids)

    def _day(d: str | None) -> str:
        # ``contests.date`` has a time suffix ("2026-09-02 16:00") but ``schedule.date`` is a bare
        # day, so all dedup compares the day portion only.
        return (d or "")[:10]

    def _prev_day(d: str) -> str:
        return (_date.fromisoformat(d) - timedelta(days=1)).isoformat() if d else ""

    def _small_hours(d: str | None) -> bool:
        # Late Hawaii/Pacific games get stored a day ahead with a small-hours time
        # ("2026-09-04 01:00" for a game really played the evening of Sep 3). The clock time is
        # the fingerprint that a contest's date crossed midnight relative to its true local day.
        t = (d or "")[11:16]
        return bool(t) and t < "06:00"

    games: list[ScoreboardGame] = []
    played_ncaa: set[str] = set()     # ncaa.com game ids of played contests (exact, date-proof key)
    played_pairs: set[tuple] = set()  # (day, {home_id, away_id}) for D1-vs-D1 games
    played_solo: set[tuple] = set()   # (day, team_id) for games vs a non-D1 (unlinked) opponent
    # Small-hours variants: the game's *true* local day is the day BEFORE the stored date, so a
    # stub on that earlier day is the same game. Keyed on that shifted-back day.
    played_pairs_shift: set[tuple] = set()  # (day-1, pair)  for small-hours D1 contests
    played_solo_shift: set[tuple] = set()   # (day-1, team_id) for small-hours non-D1 contests
    for c in contests:
        day = _day(c.date)
        if c.ncaa_game_id:
            played_ncaa.add(c.ncaa_game_id)
        pair = frozenset({c.home_team_id, c.away_team_id})
        played_pairs.add((day, pair))
        known = [x for x in (c.home_team_id, c.away_team_id) if x]
        if len(known) == 1:  # the other side is a non-D1 opponent with no Team row
            played_solo.add((day, known[0]))
        if _small_hours(c.date):
            played_pairs_shift.add((_prev_day(day), pair))
            if len(known) == 1:
                played_solo_shift.add((_prev_day(day), known[0]))
        if not (start <= day < end_excl):
            continue  # widened lookup pulled this in for dedup only; don't emit it
        games.append(ScoreboardGame(
            date=c.date, week_number=weeks.get(c.contest_id), contest_id=c.contest_id,
            ncaa_game_id=c.ncaa_game_id, status="played", home_team=refs.get(c.home_team_id),
            away_team=refs.get(c.away_team_id),
            home_sets_won=c.home_sets_won, away_sets_won=c.away_sets_won,
            set_scores=c.set_scores, attendance=c.attendance,
        ))

    seen: set[tuple] = set()
    for s in sched:
        pair = frozenset(x for x in (s.team_id, s.opponent_team_id) if x)
        day = _day(s.date)
        # 1) Exact match on the ncaa.com game id — timezone- and back-to-back-proof. Late
        #    Hawaii/Pacific games get ``contests.date`` stored a day off from ``schedule.date``,
        #    but once mapped both sides carry the same ncaa id, collapsing them regardless of day.
        if s.ncaa_game_id and s.ncaa_game_id in played_ncaa:
            continue
        if s.opponent_team_id:
            # 2) D1 matchup with no id match: drop if the pair played on the SAME day, or on the
            #    NEXT day in the small hours (a late local game whose stored date crossed midnight
            #    — its true day is this stub's day). A genuine consecutive-day rematch has the
            #    played game EARLIER than this upcoming stub, never later, so it survives both.
            if (day, pair) in played_pairs or (day, pair) in played_pairs_shift:
                continue
        elif (day, s.team_id) in played_solo or (day, s.team_id) in played_solo_shift:
            # 3) Non-D1 opponent (no id to pair on): drop if this team already has a played non-D1
            #    game this day (same-day, or a small-hours next-day contest that is really today).
            continue
        key = (s.date, pair) if s.opponent_team_id else (s.date, s.team_id, s.opponent_name)
        if key in seen:
            continue
        seen.add(key)

        team_ref, opp_ref = refs.get(s.team_id), refs.get(s.opponent_team_id)
        if s.site == "away":
            home_team, away_team = opp_ref, team_ref
            home_name = None if opp_ref else s.opponent_name
            away_name = None
        else:  # 'home' or 'neutral' — perspective team on the home slot
            home_team, away_team = team_ref, opp_ref
            home_name = None
            away_name = None if opp_ref else s.opponent_name
        games.append(ScoreboardGame(
            date=s.date, game_time=s.game_time, status="upcoming",
            contest_id=s.contest_id,
            ncaa_game_id=s.ncaa_game_id,  # links out to ncaa.com/game/<id> until the score is scraped
            neutral_location=s.neutral_location,
            home_team=home_team, away_team=away_team,
            home_name=home_name, away_name=away_name,
        ))

    # Overlay live / just-finished ncaa.com scores onto the upcoming stubs (best-effort; no-op off
    # today/yesterday). If any landed, shorten the browser cache so the ~60s frontend poll stays hot.
    if _merge_live_board(games, start, end_excl):
        response.headers["Cache-Control"] = SCOREBOARD_CACHE_CONTROL_LIVE

    # Attach network tags: one batch query, indexed by (day, unordered team pair) — the same key
    # the loader stored them under. Only D1-vs-D1 games (both sides resolved) can carry a tag.
    bx = db.scalars(
        select(Broadcast).where(
            Broadcast.season == season,
            Broadcast.game_date >= lookup_start, Broadcast.game_date < lookup_end_excl,
        )
    ).all()
    by_key: dict[tuple, list[BroadcastTag]] = defaultdict(list)
    seen_net: dict[tuple, set[str]] = defaultdict(set)
    for b in bx:
        k = (b.game_date, frozenset({b.team_a_id, b.team_b_id}))
        if b.network not in seen_net[k]:
            seen_net[k].add(b.network)
            by_key[k].append(
                BroadcastTag(network=b.network, logo_key=b.logo_key, channel_no=b.channel_no)
            )
    if by_key:
        for g in games:
            if g.home_team and g.away_team:
                tags = by_key.get((_day(g.date), frozenset({g.home_team.id, g.away_team.id})))
                if tags:
                    g.broadcasts = tags

    games.sort(key=lambda g: (g.date or "9999", g.game_time or "", g.contest_id or ""))
    return games
