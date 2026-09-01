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


class WeekOut(BaseModel):
    """One season-anchored Mon–Sun week (week_number is null for the 'Unknown' bucket)."""
    week_number: int | None = None
    start: str | None = None   # Monday (YYYY-MM-DD)
    end: str | None = None     # Sunday (YYYY-MM-DD)
    contest_count: int = 0


class LeaderRow(BaseModel):
    """A ranked player row for a stat/fantasy leaderboard. ``value`` is the ranked metric."""
    player_id: int
    name: str
    team_id: int | None = None
    team: str | None = None
    conference: str | None = None
    position: str | None = None
    games: int | None = None
    sets: float | None = None
    value: float | None = None


class TeamStatRow(BaseModel):
    """Team-aggregate stat line (sum of the roster's game stats over the season/week scope)."""
    team_id: int
    team: str
    conference: str | None = None
    games: int | None = None
    kills: float | None = None
    assists: float | None = None
    aces: float | None = None
    digs: float | None = None
    total_blocks: float | None = None
    pts: float | None = None
    fantasy_points: float | None = None


class PlayerStatLine(BaseModel):
    """A player's full stat line for a team roster table (season or week scope)."""
    player_id: int
    name: str
    position: str | None = None
    games: int | None = None
    sets: float | None = None
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
    bhe: float | None = None
    pts: float | None = None
    fantasy_points: float | None = None


class SearchOut(BaseModel):
    players: list[PlayerOut] = []
    teams: list[TeamOut] = []


class GameLogRow(BaseModel):
    """A player's single-game line, enriched with date/week/opponent for the fantasy card."""
    contest_id: str
    date: str | None = None
    week_number: int | None = None
    opponent_id: int | None = None
    opponent: str | None = None
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
    total_blocks: float | None = None
    berr: float | None = None
    pts: float | None = None
    bhe: float | None = None
    fantasy_points: float | None = None
