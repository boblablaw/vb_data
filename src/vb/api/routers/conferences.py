"""Conference endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Conference
from ..deps import get_session
from ..schemas import ConferenceOut, ConferenceSummaryOut, ConfStandingRow
from .stats import _season, load_team_records

router = APIRouter(prefix="/conferences", tags=["conferences"])


@router.get("", response_model=list[ConferenceOut])
def list_conferences(db: Session = Depends(get_session)):
    return db.scalars(select(Conference).order_by(Conference.name)).all()


@router.get("/{conference_id}/summary", response_model=ConferenceSummaryOut)
def conference_summary(
    conference_id: int,
    season: int | None = None,
    db: Session = Depends(get_session),
) -> ConferenceSummaryOut:
    """Season snapshot for one conference: standings (by conf W-L), aggregate + inter-conf records."""
    conf = db.get(Conference, conference_id)
    if conf is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conference not found.")
    season = _season(season)
    records, teams = load_team_records(db, season)
    members = [r for r in records if teams.get(r["team_id"], {}).get("conference_id") == conference_id]
    # Rank by conference record (true standings once conf play starts); before then every conf
    # record is 0-0, so overall record (then set%) breaks the tie so the order is still meaningful.
    members.sort(key=lambda r: (
        -r["conf_wins"], r["conf_losses"], -r["wins"], r["losses"], -(r["set_pct"] or 0),
    ))

    standings = [
        ConfStandingRow(
            team_id=r["team_id"], team=r["team"], team_short=r["team_short"],
            team_logo_light=r["team_logo_light"], team_logo_dark=r["team_logo_dark"],
            conf_wins=r["conf_wins"], conf_losses=r["conf_losses"],
            wins=r["wins"], losses=r["losses"], set_pct=r["set_pct"],
            rpi_rank=r["rpi_rank"], avca_rank=r["avca_rank"],
        )
        for r in members
    ]
    rpis = [r["rpi_rank"] for r in members if r["rpi_rank"] is not None]
    return ConferenceSummaryOut(
        id=conf.id, name=conf.name, short_name=conf.short_name, season=season,
        team_count=len(members),
        ranked_count=sum(1 for r in members if r["avca_rank"] is not None),
        avg_rpi_rank=round(sum(rpis) / len(rpis), 1) if rpis else None,
        overall_wins=sum(r["wins"] for r in members),
        overall_losses=sum(r["losses"] for r in members),
        interconf_wins=sum(r["nonconf_wins"] for r in members),
        interconf_losses=sum(r["nonconf_losses"] for r in members),
        standings=standings,
    )
