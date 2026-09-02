"""SQLAlchemy 2.0 ORM models (Postgres).

Schema is created/managed by Alembic (see migrations/), NOT Base.metadata.create_all —
in particular ``player_season_stats`` is a MATERIALIZED VIEW created in the migration and
merely *mapped* here for querying.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
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
    website: Mapped[str | None] = mapped_column(String)     # official athletics roster page
    stats_url: Mapped[str | None] = mapped_column(String)   # team stats page
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


class Schedule(Base):
    """A team's scheduled game (upcoming or played) scraped from its NCAA team page.

    One row per team *perspective*: a head-to-head appears on both teams' pages, so it yields two
    rows (fine for per-team queries; deduped for the league scoreboard). Played detail — scores and
    the box score — always comes from ``contests``; ``schedule`` is the source for UPCOMING games
    (which have no ``contest_id`` yet) and for opponent/site labeling. ``opponent_team_id`` is
    resolved by normalized name match and is NULL for non-D1 opponents (shown as plain text).
    """
    __tablename__ = "schedule"
    __table_args__ = (
        UniqueConstraint("season", "team_id", "date", "opponent_name", name="uq_schedule"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    opponent_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    opponent_name: Mapped[str] = mapped_column(String, nullable=False)
    date: Mapped[str] = mapped_column(String, nullable=False)  # YYYY-MM-DD
    game_time: Mapped[str | None] = mapped_column(String)      # e.g. "07:30 PM"
    site: Mapped[str | None] = mapped_column(String)           # 'home' | 'away' | 'neutral'
    neutral_location: Mapped[str | None] = mapped_column(String)
    result_raw: Mapped[str | None] = mapped_column(String)     # e.g. "W 3-1" (fallback text only)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())


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


# --------------------------------------------------------------------------- accounts / app state
# These are the ONLY tables the API writes to (the stats tables above are populated by the
# scrape/load pipeline and read-only from the web app). The prod API connects as the
# least-privilege `vb_app` role which has write grants only on this group.


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    # Null for passkey-only accounts (registered a passkey, never set a password).
    password_hash: Mapped[str | None] = mapped_column(String)
    name: Mapped[str | None] = mapped_column(String)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Per-user personalization (migrated from browser localStorage). fantasy_weights maps
    # stat -> weight; prefs holds theme/compare and other small UI state.
    fantasy_weights: Mapped[dict | None] = mapped_column(JSONB)
    prefs: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    credentials: Mapped[list[PasskeyCredential]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    favorites: Mapped[list[Favorite]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    ask_messages: Mapped[list[AskMessage]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class PasskeyCredential(Base):
    __tablename__ = "passkey_credentials"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # base64url credential id + COSE public key bytes (Yubico/py-webauthn convention).
    credential_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    user_handle: Mapped[str | None] = mapped_column(String)
    display_name: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="credentials")


class EmailVerification(Base):
    __tablename__ = "email_verifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "entity_type", "entity_id", name="uq_favorite"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)  # 'player' | 'team'
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="favorites")


class AskMessage(Base):
    """One turn of a user's in-app "Ask" conversation. A user has a single ongoing thread —
    all their rows ordered by id form the conversation; "New chat" deletes them. Only the plain
    Q&A text is stored (intermediate tool_use/tool_result blocks are re-derived per request)."""
    __tablename__ = "ask_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String, nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tools: Mapped[list | None] = mapped_column(JSONB)  # tool names used (assistant turns)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="ask_messages")


class AppSetting(Base):
    """Admin-managed runtime settings (key/value). Holds the single admin-only Anthropic API key
    (``anthropic_api_key_global``) and the MCP access token (``mcp_token``). Secret values are
    never returned to clients — the admin API only exposes ``has_*`` booleans."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
