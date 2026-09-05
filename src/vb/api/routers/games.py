"""League-wide scoreboard: played contests + upcoming scheduled games for a date/range/week."""
from __future__ import annotations

from datetime import date as _date
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Contest, ContestWeek, Schedule
from ..deps import get_session
from ..schemas import ScoreboardGame
from .contests import _team_refs

router = APIRouter(prefix="/games", tags=["games"])

# The scoreboard is user-independent and slow-changing (scores trickle in over minutes, not
# seconds), so let the browser serve a fresh copy instantly and revalidate in the background.
SCOREBOARD_CACHE_CONTROL = "public, max-age=120, stale-while-revalidate=600"


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

    def _eff_day(d: str | None) -> str:
        # A late-evening game in Hawaii/Pacific gets its ``contests.date`` rolled forward into the
        # next calendar day's small hours (e.g. "2026-09-04 01:00" for a Sep-3 night match), while
        # ``schedule.date`` keeps the real local day. Treat any contest starting before 06:00 as
        # belonging to the previous day so it dedups against the stub. A genuine back-to-back
        # rematch starts in the afternoon/evening, so its effective day stays put and its stub is
        # NOT dropped.
        day, t = (d or "")[:10], (d or "")[11:16]
        if day and t and t < "06:00":
            return (_date.fromisoformat(day) - timedelta(days=1)).isoformat()
        return day

    games: list[ScoreboardGame] = []
    played_pairs: set[tuple] = set()  # (eff_day, {home_id, away_id}) for D1-vs-D1 games
    played_solo: set[tuple] = set()   # (eff_day, team_id) for games vs a non-D1 (unlinked) opponent
    for c in contests:
        eff = _eff_day(c.date)
        played_pairs.add((eff, frozenset({c.home_team_id, c.away_team_id})))
        known = [x for x in (c.home_team_id, c.away_team_id) if x]
        if len(known) == 1:  # the other side is a non-D1 opponent with no Team row
            played_solo.add((eff, known[0]))
        if not (start <= _day(c.date) < end_excl):
            continue  # widened lookup pulled this in for dedup only; don't emit it
        games.append(ScoreboardGame(
            date=c.date, week_number=weeks.get(c.contest_id), contest_id=c.contest_id,
            ncaa_game_id=c.ncaa_game_id, status="played", home_team=refs.get(c.home_team_id),
            away_team=refs.get(c.away_team_id),
            home_sets_won=c.home_sets_won, away_sets_won=c.away_sets_won,
            set_scores=c.set_scores,
        ))

    seen: set[tuple] = set()
    for s in sched:
        pair = frozenset(x for x in (s.team_id, s.opponent_team_id) if x)
        day = _day(s.date)
        if s.opponent_team_id:
            # D1 matchup: drop the stub if the same pair has a played contest on this (real) day.
            if (day, pair) in played_pairs:
                continue
        elif (day, s.team_id) in played_solo:
            # Non-D1 opponent (no id to pair on): drop if this team already has a played non-D1
            # game this day (that IS this game — its box score just posted).
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

    games.sort(key=lambda g: (g.date or "9999", g.game_time or "", g.contest_id or ""))
    return games
