"""Contest endpoints: list by season + per-contest player stat lines."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Contest, Player, PlayerGameStat, Team
from ..deps import get_session
from ..schemas import ContestOut, GameStatOut, TeamRef

router = APIRouter(prefix="/contests", tags=["contests"])


def _team_refs(db: Session, *team_ids: int | None) -> dict[int, TeamRef]:
    ids = {t for t in team_ids if t is not None}
    if not ids:
        return {}
    rows = db.execute(
        select(Team.id, Team.name, Team.short_name, Team.logo_light, Team.logo_dark,
               Team.avca_rank)
        .where(Team.id.in_(ids))
    ).all()
    return {
        r.id: TeamRef(id=r.id, name=r.name, short_name=r.short_name,
                      logo_light=r.logo_light, logo_dark=r.logo_dark,
                      avca_rank=r.avca_rank)
        for r in rows
    }


def _contest_out(c: Contest, refs: dict[int, TeamRef]) -> ContestOut:
    return ContestOut(
        contest_id=c.contest_id, season=c.season, date=c.date,
        home_team_id=c.home_team_id, away_team_id=c.away_team_id,
        home_sets_won=c.home_sets_won, away_sets_won=c.away_sets_won,
        set_scores=c.set_scores,
        home_team=refs.get(c.home_team_id), away_team=refs.get(c.away_team_id),
    )


@router.get("", response_model=list[ContestOut])
def list_contests(
    season: int = Query(...),
    limit: int = Query(200, le=5000),
    offset: int = 0,
    db: Session = Depends(get_session),
):
    contests = db.scalars(
        select(Contest).where(Contest.season == season)
        .order_by(Contest.contest_id).limit(limit).offset(offset)
    ).all()
    refs = _team_refs(db, *[c.home_team_id for c in contests],
                      *[c.away_team_id for c in contests])
    return [_contest_out(c, refs) for c in contests]


@router.get("/{contest_id}", response_model=ContestOut)
def get_contest(contest_id: str, db: Session = Depends(get_session)):
    c = db.get(Contest, contest_id)
    if c is None:
        raise HTTPException(404, "contest not found")
    return _contest_out(c, _team_refs(db, c.home_team_id, c.away_team_id))


@router.get("/{contest_id}/stats", response_model=list[GameStatOut])
def contest_stats(contest_id: str, db: Session = Depends(get_session)):
    rows = db.execute(
        select(PlayerGameStat, Player.name, Player.position, Player.height_inches)
        .join(Player, Player.id == PlayerGameStat.player_id, isouter=True)
        .where(PlayerGameStat.contest_id == contest_id)
    ).all()
    out: list[GameStatOut] = []
    for pgs, name, position, height_inches in rows:
        line = GameStatOut.model_validate(pgs)
        line.player_name = name
        line.position = position
        line.height_inches = height_inches
        out.append(line)
    return out
