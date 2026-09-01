"""SQLAlchemy 2.0 ORM models (Postgres).

Schema is created/managed by Alembic (see migrations/), NOT Base.metadata.create_all —
in particular ``player_season_stats`` is a MATERIALIZED VIEW created in the migration and
merely *mapped* here for querying.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Conference(Base):
    __tablename__ = "conferences"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Editable short form / abbreviation (e.g. "SEC", "MAC", "Pac-12"); null when the conference
    # has no distinct abbreviation (the UI falls back to the trimmed name). Not touched by
    # load-teams, so manual edits persist across reloads.
    short_name: Mapped[str | None] = mapped_column(String, nullable=True)
    teams: Mapped[list[Team]] = relationship(back_populates="conference")


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    short_name: Mapped[str | None] = mapped_column(String)
    conference_id: Mapped[int | None] = mapped_column(ForeignKey("conferences.id"))
    city: Mapped[str | None] = mapped_column(String)
    state: Mapped[str | None] = mapped_column(String)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    logo_light: Mapped[str | None] = mapped_column(String)
    logo_dark: Mapped[str | None] = mapped_column(String)
    aliases: Mapped[list | None] = mapped_column(JSONB)
    rpi_rank: Mapped[int | None] = mapped_column(Integer)
    rpi_record: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    conference: Mapped[Conference | None] = relationship(back_populates="teams")
    season_ids: Mapped[list[TeamSeasonId]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )
    players: Mapped[list[Player]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )
    coaches: Mapped[list[Coach]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )


class TeamSeasonId(Base):
    """Maps a team to its (season-specific) NCAA team id. NCAA ids change every season."""
    __tablename__ = "team_season_ids"
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    ncaa_team_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    team: Mapped[Team] = relationship(back_populates="season_ids")


class Coach(Base):
    __tablename__ = "coaches"
    __table_args__ = (UniqueConstraint("team_id", "name", "title", "season", name="uq_coach"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    season: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String)
    sort_order: Mapped[int | None] = mapped_column(Integer)
    # NCAA-sourced head-coach fields (from the roster page "Coach" card).
    ncaa_coach_id: Mapped[str | None] = mapped_column(String, index=True)
    seasons: Mapped[str | None] = mapped_column(String)  # tenure, e.g. "5th"
    record: Mapped[str | None] = mapped_column(String)   # career record, e.g. "120-45"
    team: Mapped[Team] = relationship(back_populates="coaches")


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        UniqueConstraint("ncaa_player_id", "season", name="uq_player_ncaa_season"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    ncaa_player_id: Mapped[str | None] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    number: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[str | None] = mapped_column(String)
    class_year: Mapped[str | None] = mapped_column(String)
    height_inches: Mapped[int | None] = mapped_column(Integer)
    hometown: Mapped[str | None] = mapped_column(String)
    high_school: Mapped[str | None] = mapped_column(String)
    photo_path: Mapped[str | None] = mapped_column(String)

    team: Mapped[Team] = relationship(back_populates="players")
    game_stats: Mapped[list[PlayerGameStat]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )


class Contest(Base):
    __tablename__ = "contests"
    contest_id: Mapped[str] = mapped_column(String, primary_key=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    date: Mapped[str | None] = mapped_column(String)
    home_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    # Match result from the linescore: sets won per side (winner = more) + per-set point
    # totals as {"away": [..], "home": [..]}. NULL for unplayed/unparsed contests.
    home_sets_won: Mapped[int | None] = mapped_column(Integer)
    away_sets_won: Mapped[int | None] = mapped_column(Integer)
    set_scores: Mapped[dict | None] = mapped_column(JSONB)


class ContestWeek(Base):
    """DERIVED season-anchored week per contest — mapped to the VIEW contest_weeks.

    Created in the Alembic migration (0002) as a live view over ``contests``; read-only from the
    app. ``week_number`` is 1-based per season (Week 1 = the Monday-based week of the season's first
    match) and is NULL when the contest date is missing/unparseable ("Unknown" bucket).
    """
    __tablename__ = "contest_weeks"
    contest_id: Mapped[str] = mapped_column(primary_key=True)
    season: Mapped[int] = mapped_column(Integer)
    game_date: Mapped[date | None] = mapped_column(Date)
    week_monday: Mapped[date | None] = mapped_column(Date)
    week_number: Mapped[int | None] = mapped_column(Integer)


# The counting-stat columns shared by the per-game fact and the (scraped) season table.
_COUNTING = (
    "sets", "kills", "errors", "total_attacks", "assists", "aces", "serr",
    "digs", "retatt", "rerr", "block_solos", "block_assists", "berr", "pts", "bhe",
)


class PlayerGameStat(Base):
    """Raw per-contest per-player fact — the PRIMARY stat source. Cumulative is derived."""
    __tablename__ = "player_game_stats"
    contest_id: Mapped[str] = mapped_column(
        ForeignKey("contests.contest_id", ondelete="CASCADE"), primary_key=True
    )
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    sets: Mapped[float | None] = mapped_column(Float)
    kills: Mapped[float | None] = mapped_column(Float)
    errors: Mapped[float | None] = mapped_column(Float)
    total_attacks: Mapped[float | None] = mapped_column(Float)
    assists: Mapped[float | None] = mapped_column(Float)
    aces: Mapped[float | None] = mapped_column(Float)
    serr: Mapped[float | None] = mapped_column(Float)
    digs: Mapped[float | None] = mapped_column(Float)
    retatt: Mapped[float | None] = mapped_column(Float)
    rerr: Mapped[float | None] = mapped_column(Float)
    block_solos: Mapped[float | None] = mapped_column(Float)
    block_assists: Mapped[float | None] = mapped_column(Float)
    berr: Mapped[float | None] = mapped_column(Float)
    pts: Mapped[float | None] = mapped_column(Float)
    bhe: Mapped[float | None] = mapped_column(Float)

    player: Mapped[Player] = relationship(back_populates="game_stats")


class PlayerSeasonStatScraped(Base):
    """Season-to-date totals scraped from NCAA. VALIDATION ONLY — not the app source."""
    __tablename__ = "player_season_stats_scraped"
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    gp: Mapped[float | None] = mapped_column(Float)
    gs: Mapped[float | None] = mapped_column(Float)
    sp: Mapped[float | None] = mapped_column(Float)
    kills: Mapped[float | None] = mapped_column(Float)
    errors: Mapped[float | None] = mapped_column(Float)
    total_attacks: Mapped[float | None] = mapped_column(Float)
    hit_pct: Mapped[float | None] = mapped_column(Float)
    assists: Mapped[float | None] = mapped_column(Float)
    aces: Mapped[float | None] = mapped_column(Float)
    serr: Mapped[float | None] = mapped_column(Float)
    digs: Mapped[float | None] = mapped_column(Float)
    retatt: Mapped[float | None] = mapped_column(Float)
    rerr: Mapped[float | None] = mapped_column(Float)
    block_solos: Mapped[float | None] = mapped_column(Float)
    block_assists: Mapped[float | None] = mapped_column(Float)
    berr: Mapped[float | None] = mapped_column(Float)
    pts: Mapped[float | None] = mapped_column(Float)
    bhe: Mapped[float | None] = mapped_column(Float)
    trpl_dbl: Mapped[float | None] = mapped_column(Float)


class PlayerSeasonStat(Base):
    """DERIVED cumulative stats — mapped to the MATERIALIZED VIEW player_season_stats.

    Created in the Alembic migration as a matview over player_game_stats; refreshed by
    vb.derive.cumulative. Read-only from the app's perspective.
    """
    __tablename__ = "player_season_stats"
    player_id: Mapped[int] = mapped_column(primary_key=True)
    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int | None] = mapped_column(Integer)
    gp: Mapped[int | None] = mapped_column(Integer)
    gs: Mapped[int | None] = mapped_column(Integer)  # always null (see plan: GS gap)
    sp: Mapped[float | None] = mapped_column(Float)
    kills: Mapped[float | None] = mapped_column(Float)
    errors: Mapped[float | None] = mapped_column(Float)
    total_attacks: Mapped[float | None] = mapped_column(Float)
    hit_pct: Mapped[float | None] = mapped_column(Float)
    assists: Mapped[float | None] = mapped_column(Float)
    aces: Mapped[float | None] = mapped_column(Float)
    serr: Mapped[float | None] = mapped_column(Float)
    digs: Mapped[float | None] = mapped_column(Float)
    retatt: Mapped[float | None] = mapped_column(Float)
    rerr: Mapped[float | None] = mapped_column(Float)
    block_solos: Mapped[float | None] = mapped_column(Float)
    block_assists: Mapped[float | None] = mapped_column(Float)
    total_blocks: Mapped[float | None] = mapped_column(Float)
    berr: Mapped[float | None] = mapped_column(Float)
    pts: Mapped[float | None] = mapped_column(Float)
    bhe: Mapped[float | None] = mapped_column(Float)
    kills_per_set: Mapped[float | None] = mapped_column(Float)
    assists_per_set: Mapped[float | None] = mapped_column(Float)
    aces_per_set: Mapped[float | None] = mapped_column(Float)
    digs_per_set: Mapped[float | None] = mapped_column(Float)
    blocks_per_set: Mapped[float | None] = mapped_column(Float)
    pts_per_set: Mapped[float | None] = mapped_column(Float)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    season: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str | None] = mapped_column(String)   # teams | rosters | game_stats | derive | ...
    source: Mapped[str | None] = mapped_column(String)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
