"""Conference endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Conference
from ..deps import get_session
from ..schemas import ConferenceOut

router = APIRouter(prefix="/conferences", tags=["conferences"])


@router.get("", response_model=list[ConferenceOut])
def list_conferences(db: Session = Depends(get_session)):
    return db.scalars(select(Conference).order_by(Conference.name)).all()
