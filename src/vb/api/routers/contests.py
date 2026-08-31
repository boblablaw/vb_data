"""Contest endpoints: list by season + per-contest player stat lines."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Contest, PlayerGameStat
from ..deps import get_session
from ..schemas import ContestOut, GameStatOut

router = APIRouter(prefix="/contests", tags=["contests"])


@router.get("", response_model=list[ContestOut])
def list_contests(
    season: int = Query(...),
    limit: int = Query(200, le=5000),
    offset: int = 0,
    db: Session = Depends(get_session),
):
    return db.scalars(
        select(Contest).where(Contest.season == season)
        .order_by(Contest.contest_id).limit(limit).offset(offset)
    ).all()


@router.get("/{contest_id}", response_model=ContestOut)
def get_contest(contest_id: str, db: Session = Depends(get_session)):
    c = db.get(Contest, contest_id)
    if c is None:
        raise HTTPException(404, "contest not found")
    return c


@router.get("/{contest_id}/stats", response_model=list[GameStatOut])
def contest_stats(contest_id: str, db: Session = Depends(get_session)):
    return db.scalars(
        select(PlayerGameStat).where(PlayerGameStat.contest_id == contest_id)
    ).all()
