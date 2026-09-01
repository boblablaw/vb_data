"""Team endpoints (list/detail + roster + coaches)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...models import Coach, Player, Team
from ..deps import get_session
from ..schemas import CoachOut, PlayerOut, TeamOut

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


@router.get("/{team_id}/coaches", response_model=list[CoachOut])
def team_coaches(
    team_id: int, season: int | None = None, db: Session = Depends(get_session)
):
    stmt = select(Coach).where(Coach.team_id == team_id)
    if season is not None:
        stmt = stmt.where(Coach.season == season)
    return db.scalars(stmt.order_by(Coach.sort_order)).all()
