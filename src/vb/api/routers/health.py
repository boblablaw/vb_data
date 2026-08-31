"""Health check."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_session)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
