"""Pydantic response schemas for the API (read models)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ConferenceOut(ORMModel):
    id: int
    name: str


class TeamOut(ORMModel):
    id: int
    name: str
    short_name: str | None = None
    conference: str | None = None
    city: str | None = None
    state: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    logo_light: str | None = None
    logo_dark: str | None = None
    rpi_rank: int | None = None
    rpi_record: str | None = None


class CoachOut(ORMModel):
    id: int
    name: str
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    season: int | None = None


class PlayerOut(ORMModel):
    id: int
    ncaa_player_id: str | None = None
    name: str
    season: int
    team_id: int
    team: str | None = None
    number: int | None = None
    position: str | None = None
    class_year: str | None = None
    height_inches: int | None = None
    hometown: str | None = None
    high_school: str | None = None
    photo_path: str | None = None

    @classmethod
    def from_player(cls, p) -> PlayerOut:
        return cls(
            id=p.id, ncaa_player_id=p.ncaa_player_id, name=p.name, season=p.season,
            team_id=p.team_id, team=(p.team.name if p.team else None),
            number=p.number, position=p.position, class_year=p.class_year,
            height_inches=p.height_inches, hometown=p.hometown,
            high_school=p.high_school, photo_path=p.photo_path,
        )


class SeasonStatOut(ORMModel):
    player_id: int
    season: int
    team_id: int | None = None
    gp: int | None = None
    gs: int | None = None  # served from the scraped table (matview lacks GS)
    sp: float | None = None
    kills: float | None = None
    errors: float | None = None
    total_attacks: float | None = None
    hit_pct: float | None = None
    assists: float | None = None
    aces: float | None = None
    serr: float | None = None
    digs: float | None = None
    retatt: float | None = None
    rerr: float | None = None
    block_solos: float | None = None
    block_assists: float | None = None
    total_blocks: float | None = None
    berr: float | None = None
    pts: float | None = None
    bhe: float | None = None
    kills_per_set: float | None = None
    assists_per_set: float | None = None
    aces_per_set: float | None = None
    digs_per_set: float | None = None
    blocks_per_set: float | None = None
    pts_per_set: float | None = None


class GameStatOut(ORMModel):
    contest_id: str
    player_id: int
    team_id: int
    season: int
    sets: float | None = None
    kills: float | None = None
    errors: float | None = None
    total_attacks: float | None = None
    assists: float | None = None
    aces: float | None = None
    serr: float | None = None
    digs: float | None = None
    retatt: float | None = None
    rerr: float | None = None
    block_solos: float | None = None
    block_assists: float | None = None
    berr: float | None = None
    pts: float | None = None
    bhe: float | None = None


class ContestOut(ORMModel):
    contest_id: str
    season: int
    date: str | None = None
    home_team_id: int | None = None
    away_team_id: int | None = None
