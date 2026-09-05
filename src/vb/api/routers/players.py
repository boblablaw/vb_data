"""Player endpoints: list/detail, derived season stats, per-contest game stats."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    Player,
    PlayerGameStat,
    PlayerPbpStat,
    PlayerSeasonStat,
    PlayerSeasonStatScraped,
    Team,
)
from ..deps import get_session
from ..schemas import GameStatOut, PlayerOut, SeasonStatOut

router = APIRouter(prefix="/players", tags=["players"])


def _player_out(p: Player) -> PlayerOut:
    return PlayerOut.from_player(p)


@router.get("", response_model=list[PlayerOut])
def list_players(
    db: Session = Depends(get_session),
    season: int = Query(..., description="season (fall year)"),
    team: str | None = Query(None, description="team name (exact)"),
    team_id: int | None = None,
    q: str | None = Query(None, description="player name substring"),
    limit: int = Query(200, le=2000),
    offset: int = 0,
):
    stmt = select(Player).where(Player.season == season)
    if team_id is not None:
        stmt = stmt.where(Player.team_id == team_id)
    if team:
        stmt = stmt.join(Team, Team.id == Player.team_id).where(Team.name == team)
    if q:
        stmt = stmt.where(Player.name.ilike(f"%{q}%"))
    players = db.scalars(stmt.order_by(Player.name).limit(limit).offset(offset)).all()
    return [_player_out(p) for p in players]


@router.get("/{player_id}", response_model=PlayerOut)
def get_player(player_id: int, db: Session = Depends(get_session)):
    p = db.get(Player, player_id)
    if p is None:
        raise HTTPException(404, "player not found")
    return _player_out(p)


@router.get("/{player_id}/season-stats", response_model=SeasonStatOut)
def player_season_stats(
    player_id: int, season: int | None = None, db: Session = Depends(get_session)
):
    p = db.get(Player, player_id)
    if p is None:
        raise HTTPException(404, "player not found")
    season = season if season is not None else p.season
    row = db.get(PlayerSeasonStat, (player_id, season))
    if row is None:
        raise HTTPException(404, "no derived season stats for player/season")
    out = SeasonStatOut.model_validate(row)
    # GS is not derivable from per-game data — pull it from the scraped table.
    scraped = db.get(PlayerSeasonStatScraped, (player_id, season))
    if scraped is not None and scraped.gs is not None:
        out.gs = int(scraped.gs)
    # Advanced play-by-play stats (nullable — present only once PBP is loaded/derived).
    pbp = db.get(PlayerPbpStat, (player_id, season))
    if pbp is not None:
        out.set_attempts = pbp.set_attempts
        out.assist_pct = pbp.assist_pct
        out.setter_hitting_pct = pbp.setter_hitting_pct
        out.setter_hit_attacks = pbp.setter_hit_attacks
        out.points_played = pbp.points_played
    return out


@router.get("/{player_id}/game-stats", response_model=list[GameStatOut])
def player_game_stats(
    player_id: int, season: int | None = None, db: Session = Depends(get_session)
):
    stmt = select(PlayerGameStat).where(PlayerGameStat.player_id == player_id)
    if season is not None:
        stmt = stmt.where(PlayerGameStat.season == season)
    return db.scalars(stmt.order_by(PlayerGameStat.contest_id)).all()
