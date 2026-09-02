"""Per-user favorites (players & teams). All endpoints require a signed-in user."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Favorite, Player, Team, User
from ..deps import get_session, require_user, require_verified
from ..schemas import FavoriteIn, FavoriteOut

router = APIRouter(prefix="/favorites", tags=["favorites"])

_VALID_TYPES = {"player", "team"}


def _enrich(db: Session, fav: Favorite) -> FavoriteOut:
    if fav.entity_type == "team":
        t = db.get(Team, fav.entity_id)
        if t is None:
            return FavoriteOut(entity_type="team", entity_id=fav.entity_id)
        return FavoriteOut(
            entity_type="team", entity_id=t.id, name=t.name, team_short=t.short_name,
            conference=(t.conference.short_name or t.conference.name) if t.conference else None,
            logo_light=t.logo_light, logo_dark=t.logo_dark,
        )
    p = db.get(Player, fav.entity_id)
    if p is None:
        return FavoriteOut(entity_type="player", entity_id=fav.entity_id)
    return FavoriteOut(
        entity_type="player", entity_id=p.id, name=p.name, position=p.position,
        team=(p.team.name if p.team else None),
        team_short=(p.team.short_name if p.team else None),
    )


@router.get("", response_model=list[FavoriteOut])
def list_favorites(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> list[FavoriteOut]:
    favs = db.scalars(
        select(Favorite).where(Favorite.user_id == user.id).order_by(Favorite.created_at.desc())
    ).all()
    return [_enrich(db, f) for f in favs]


@router.post("", response_model=FavoriteOut, status_code=status.HTTP_201_CREATED)
def add_favorite(
    body: FavoriteIn,
    user: User = Depends(require_verified),
    db: Session = Depends(get_session),
) -> FavoriteOut:
    if body.entity_type not in _VALID_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "entity_type must be 'player' or 'team'.")
    existing = db.scalar(
        select(Favorite).where(
            Favorite.user_id == user.id,
            Favorite.entity_type == body.entity_type,
            Favorite.entity_id == body.entity_id,
        )
    )
    if existing is None:
        existing = Favorite(
            user_id=user.id, entity_type=body.entity_type, entity_id=body.entity_id
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)
    return _enrich(db, existing)


@router.delete("/{entity_type}/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_favorite(
    entity_type: str,
    entity_id: int,
    user: User = Depends(require_verified),
    db: Session = Depends(get_session),
) -> None:
    db.query(Favorite).filter(
        Favorite.user_id == user.id,
        Favorite.entity_type == entity_type,
        Favorite.entity_id == entity_id,
    ).delete()
    db.commit()
