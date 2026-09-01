"""Shared FastAPI dependencies: DB session + optional/required auth.

Read endpoints stay public (no auth). ``get_current_user`` returns the signed-in ``User`` or
``None`` (never raises), so a route can personalize output for anonymous callers. ``require_user``
and ``require_admin`` gate the new write/admin endpoints.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from ..auth.security import decode_token
from ..db import get_session
from ..models import User

__all__ = ["get_current_user", "get_session", "require_admin", "require_user"]


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_session),
) -> User | None:
    """Resolve the bearer token to a User, or None if absent/invalid."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_token(token)
    if not claims:
        return None
    try:
        user_id = int(claims.get("sub", ""))
    except (TypeError, ValueError):
        return None
    return db.get(User, user_id)


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user
