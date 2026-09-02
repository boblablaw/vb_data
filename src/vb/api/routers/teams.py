"""Team endpoints (list/detail + roster + coaches)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...models import Coach, Contest, ContestWeek, Player, Schedule, Team
from ..deps import get_session
from ..schemas import CoachOut, PlayerOut, TeamGameRow, TeamOut

router = APIRouter(prefix="/teams", tags=["teams"])


def _to_out(team: Team) -> TeamOut:
    return TeamOut(
        id=team.id, name=team.name, short_name=team.short_name,
        conference=team.conference.name if team.conference else None,
        city=team.city, state=team.state,
        latitude=team.latitude, longitude=team.longitude,
        logo_light=team.logo_light, logo_dark=team.logo_dark,
        website=team.website, stats_url=team.stats_url,
        rpi_rank=team.rpi_rank, rpi_record=team.rpi_record,
    )


@router.get("", response_model=list[TeamOut])
def list_teams(
    db: Session = Depends(get_session),
    conference: str | None = None,
    state: str | None = None,
    q: str | None = Query(None, description="name substring"),
):
    stmt = select(Team)
    if state:
        stmt = stmt.where(Team.state == state)
    if q:
        stmt = stmt.where(or_(Team.name.ilike(f"%{q}%"), Team.short_name.ilike(f"%{q}%")))
    teams = db.scalars(stmt.order_by(Team.name)).all()
    if conference:
        teams = [t for t in teams if t.conference and t.conference.name == conference]
    return [_to_out(t) for t in teams]


@router.get("/{team_id}", response_model=TeamOut)
def get_team(team_id: int, db: Session = Depends(get_session)):
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "team not found")
    return _to_out(team)


@router.get("/{team_id}/roster", response_model=list[PlayerOut])
def team_roster(team_id: int, season: int, db: Session = Depends(get_session)):
    players = db.scalars(
        select(Player).where(Player.team_id == team_id, Player.season == season)
        .order_by(Player.number)
    ).all()
    return [PlayerOut.from_player(p) for p in players]


@router.get("/{team_id}/games", response_model=list[TeamGameRow])
def team_games(team_id: int, season: int, db: Session = Depends(get_session)):
    """A team's season games, date-ordered: played (from ``contests``) + upcoming (from ``schedule``).

    Played games are authoritative (scores + a box-score ``contest_id``); upcoming games come from
    ``schedule`` rows that have no result yet. Opponent name/logo are resolved when the opponent is
    a known D1 team, else the raw schedule name is shown with no link.
    """
    # --- Played games from contests (+ week number from the contest_weeks view) ---
    played_rows = db.execute(
        select(Contest, ContestWeek.week_number)
        .join(ContestWeek, ContestWeek.contest_id == Contest.contest_id, isouter=True)
        .where(
            Contest.season == season,
            or_(Contest.home_team_id == team_id, Contest.away_team_id == team_id),
        )
    ).all()

    # --- Upcoming games from schedule (no result yet) ---
    upcoming = db.scalars(
        select(Schedule).where(
            Schedule.season == season,
            Schedule.team_id == team_id,
            Schedule.result_raw.is_(None),
        )
    ).all()

    # Resolve every referenced opponent team in one query.
    opp_ids: set[int] = set()
    for c, _wk in played_rows:
        opp_ids.add(c.home_team_id if c.away_team_id == team_id else c.away_team_id)
    for s in upcoming:
        if s.opponent_team_id is not None:
            opp_ids.add(s.opponent_team_id)
    opp_ids.discard(None)
    teams: dict[int, Team] = {}
    if opp_ids:
        teams = {t.id: t for t in db.scalars(select(Team).where(Team.id.in_(opp_ids))).all()}

    played_dates: set[str] = set()
    out: list[TeamGameRow] = []
    for c, wk in played_rows:
        is_home = c.home_team_id == team_id
        opp_id = c.away_team_id if is_home else c.home_team_id
        opp = teams.get(opp_id)
        team_won = (c.home_sets_won if is_home else c.away_sets_won)
        opp_won = (c.away_sets_won if is_home else c.home_sets_won)
        result = None
        if team_won is not None and opp_won is not None:
            result = "W" if team_won > opp_won else "L"
        if c.date:
            played_dates.add(c.date)
        out.append(TeamGameRow(
            date=c.date, week_number=wk, site="home" if is_home else "away",
            contest_id=c.contest_id, opponent_id=opp_id,
            opponent=opp.name if opp else None,
            opponent_short=opp.short_name if opp else None,
            opponent_logo_light=opp.logo_light if opp else None,
            opponent_logo_dark=opp.logo_dark if opp else None,
            result=result, team_sets_won=team_won, opponent_sets_won=opp_won,
            status="played",
        ))

    for s in upcoming:
        if s.date in played_dates:  # already represented by a played contest
            continue
        opp = teams.get(s.opponent_team_id)
        out.append(TeamGameRow(
            date=s.date, game_time=s.game_time, site=s.site,
            neutral_location=s.neutral_location, opponent_id=s.opponent_team_id,
            opponent=opp.name if opp else s.opponent_name,
            opponent_short=opp.short_name if opp else None,
            opponent_logo_light=opp.logo_light if opp else None,
            opponent_logo_dark=opp.logo_dark if opp else None,
            status="upcoming",
        ))

    out.sort(key=lambda g: (g.date or "9999", g.game_time or ""))
    return out


@router.get("/{team_id}/coaches", response_model=list[CoachOut])
def team_coaches(
    team_id: int, season: int | None = None, db: Session = Depends(get_session)
):
    stmt = select(Coach).where(Coach.team_id == team_id)
    if season is not None:
        stmt = stmt.where(Coach.season == season)
    return db.scalars(stmt.order_by(Coach.sort_order)).all()
