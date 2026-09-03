"""Pydantic response schemas for the API (read models)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ConferenceOut(ORMModel):
    id: int
    name: str
    short_name: str | None = None


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
    website: str | None = None
    stats_url: str | None = None
    rpi_rank: int | None = None
    rpi_record: str | None = None
    avca_rank: int | None = None


class CoachOut(ORMModel):
    id: int
    name: str
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    season: int | None = None
    ncaa_coach_id: str | None = None
    seasons: str | None = None
    record: str | None = None


class PlayerOut(ORMModel):
    id: int
    ncaa_player_id: str | None = None
    name: str
    season: int
    team_id: int
    team: str | None = None
    team_short: str | None = None
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
            team_short=(p.team.short_name if p.team else None),
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
    player_name: str | None = None
    position: str | None = None
    height_inches: int | None = None
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


class TeamRef(BaseModel):
    """Compact team reference for embedding in game headers/rows (name + logos, no stats)."""
    id: int
    name: str
    short_name: str | None = None
    logo_light: str | None = None
    logo_dark: str | None = None
    avca_rank: int | None = None
    conference_id: int | None = None


class ContestOut(ORMModel):
    contest_id: str
    season: int
    date: str | None = None
    home_team_id: int | None = None
    away_team_id: int | None = None
    home_sets_won: int | None = None
    away_sets_won: int | None = None
    set_scores: dict | None = None            # {"home": [25, 23, ...], "away": [...]}
    home_team: TeamRef | None = None
    away_team: TeamRef | None = None


class TeamGameRow(BaseModel):
    """One game on a team's schedule — played (has contest_id + result) or upcoming (no id)."""
    date: str | None = None
    game_time: str | None = None
    week_number: int | None = None
    site: str | None = None                   # 'home' | 'away' | 'neutral'
    neutral_location: str | None = None
    contest_id: str | None = None             # present only for played games
    opponent_id: int | None = None
    opponent: str | None = None
    opponent_short: str | None = None
    opponent_logo_light: str | None = None
    opponent_logo_dark: str | None = None
    opponent_avca_rank: int | None = None
    result: str | None = None                 # 'W' | 'L' | None (upcoming)
    team_sets_won: int | None = None
    opponent_sets_won: int | None = None
    set_scores: dict | None = None            # raw {"home": [...], "away": [...]}; client orients by site
    status: str = "upcoming"                   # 'played' | 'upcoming'


class ScoreboardGame(BaseModel):
    """One game in the league-wide scoreboard (deduped across the two per-team perspectives)."""
    date: str | None = None
    game_time: str | None = None
    week_number: int | None = None
    contest_id: str | None = None
    status: str = "upcoming"                   # 'played' | 'upcoming'
    neutral_location: str | None = None
    home_team: TeamRef | None = None
    away_team: TeamRef | None = None
    home_name: str | None = None              # fallback display when a side is unresolved
    away_name: str | None = None
    home_sets_won: int | None = None
    away_sets_won: int | None = None
    set_scores: dict | None = None            # {"home": [25, 23, ...], "away": [...]}


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
    team_short: str | None = None
    team_logo_light: str | None = None
    team_logo_dark: str | None = None
    conference: str | None = None
    position: str | None = None
    class_year: str | None = None
    height_inches: int | None = None
    games: int | None = None
    sets: float | None = None
    value: float | None = None
    # Per-category detail stats (kills, aces, block_solos, ...) so the UI can render each NCAA
    # stat page's bespoke column set. Keys are matview column names; see LEADER_COMPONENTS.
    components: dict[str, float | None] = {}


class TeamStatRow(BaseModel):
    """Team-aggregate stat line (sum of the roster's game stats over the season/week scope)."""
    team_id: int
    team: str
    team_short: str | None = None
    conference: str | None = None
    games: int | None = None
    kills: float | None = None
    assists: float | None = None
    aces: float | None = None
    digs: float | None = None
    total_blocks: float | None = None
    pts: float | None = None
    fantasy_points: float | None = None


class TeamRecordRow(BaseModel):
    """A team's season record for the standings page (derived from contest linescores)."""
    team_id: int
    team: str
    team_short: str | None = None
    team_logo_light: str | None = None
    team_logo_dark: str | None = None
    conference: str | None = None
    games: int = 0
    wins: int = 0
    losses: int = 0
    sets_won: int = 0
    sets_lost: int = 0
    set_pct: float | None = None          # sets_won / (sets_won + sets_lost)
    conf_wins: int = 0
    conf_losses: int = 0
    nonconf_wins: int = 0
    nonconf_losses: int = 0
    opp_wins: int = 0                      # opponents' combined record, excluding head-to-head
    opp_losses: int = 0
    opp_rpi: float | None = None           # mean RPI rank of opponents faced (lower = tougher)
    win_streak: int = 0                    # signed run from most recent game (+wins / −losses)
    rpi_rank: int | None = None
    rpi_record: str | None = None
    avca_rank: int | None = None


class ConfStandingRow(BaseModel):
    """One member team in a conference-summary standings list (ordered by conference W-L)."""
    team_id: int
    team: str
    team_short: str | None = None
    team_logo_light: str | None = None
    team_logo_dark: str | None = None
    conf_wins: int = 0
    conf_losses: int = 0
    wins: int = 0
    losses: int = 0
    set_pct: float | None = None
    rpi_rank: int | None = None
    avca_rank: int | None = None


class ConferenceSummaryOut(BaseModel):
    """Aggregate season snapshot of a single conference (drives the Favorites conference card)."""
    id: int
    name: str
    short_name: str | None = None
    season: int
    team_count: int = 0
    ranked_count: int = 0                   # members carrying an AVCA rank
    avg_rpi_rank: float | None = None       # mean RPI rank across members (lower = stronger)
    overall_wins: int = 0
    overall_losses: int = 0
    interconf_wins: int = 0                  # combined record vs other D1 conferences
    interconf_losses: int = 0
    standings: list[ConfStandingRow] = []    # all members, best conference record first


class PlayerStatLine(BaseModel):
    """A player's full stat line for a team roster table (season or week scope)."""
    player_id: int
    name: str
    position: str | None = None
    height_inches: int | None = None
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
    kills_per_set: float | None = None
    assists_per_set: float | None = None
    aces_per_set: float | None = None
    digs_per_set: float | None = None
    blocks_per_set: float | None = None
    pts_per_set: float | None = None
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
    opponent_short: str | None = None
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


# --------------------------------------------------------------------------- accounts / auth
class UserOut(BaseModel):
    """Public view of a user account. Never exposes secrets — only ``has_*`` flags for them."""
    id: int
    email: str
    name: str | None = None
    is_admin: bool = False
    email_verified: bool = False
    fantasy_weights: dict | None = None
    prefs: dict | None = None

    @classmethod
    def from_user(cls, u) -> UserOut:
        return cls(
            id=u.id, email=u.email, name=u.name, is_admin=u.is_admin,
            email_verified=u.email_verified, fantasy_weights=u.fantasy_weights, prefs=u.prefs,
        )


class AuthOut(BaseModel):
    """Login/register/passkey response: a bearer token + the user profile."""
    token: str
    user: UserOut


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: str | None = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class EmailIn(BaseModel):
    """Request a magic sign-in link for this email."""
    email: EmailStr


class TokenIn(BaseModel):
    """Consume a magic sign-in link."""
    token: str


class UpdateMeIn(BaseModel):
    name: str | None = None
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=8)
    fantasy_weights: dict | None = None
    prefs: dict | None = None


# --------------------------------------------------------------------------- favorites
class FavoriteIn(BaseModel):
    entity_type: str  # 'player' | 'team'
    entity_id: int


class FavoriteOut(BaseModel):
    entity_type: str
    entity_id: int
    name: str | None = None
    team: str | None = None          # for players: their team name
    team_id: int | None = None       # for players: their team id (drives per-game favorite counts)
    team_short: str | None = None
    conference: str | None = None
    logo_light: str | None = None
    logo_dark: str | None = None
    position: str | None = None


class FavoriteContestsOut(BaseModel):
    """Games involving the user's favorite players (drives the Games "Favorite players" filter).

    ``contest_ids`` are played contests those players appeared in; ``team_ids`` are their teams, used
    to also match *upcoming* games (which have no box score / contest yet).
    """
    contest_ids: list[str] = []
    team_ids: list[int] = []


# ------------------------------------------------------------------- quality wins
class QualityWinOut(BaseModel):
    opponent_id: int | None = None
    opponent: str | None = None
    opponent_short: str | None = None
    opponent_logo_light: str | None = None
    opponent_logo_dark: str | None = None
    rank_at_time: int
    poll: str
    date: str | None = None
    score: str | None = None
    contest_id: str | None = None


class TeamQualityWinsOut(BaseModel):
    team_id: int
    poll: str
    threshold: int
    quality_wins: int
    wins: list[QualityWinOut] = []


# --------------------------------------------------------------------------- admin
class AdminUserOut(BaseModel):
    id: int
    email: str
    name: str | None = None
    is_admin: bool = False
    email_verified: bool = False
    created_at: str | None = None


class AdminUserPatchIn(BaseModel):
    is_admin: bool | None = None
    email_verified: bool | None = None


class AdminSettingsOut(BaseModel):
    has_mcp_token: bool = False
    has_global_ai_key: bool = False


class AdminSettingsIn(BaseModel):
    # None = leave unchanged; "" = clear; any other string = set.
    mcp_token: str | None = None
    anthropic_api_key_global: str | None = None


class SignupDay(BaseModel):
    date: str            # ISO date (YYYY-MM-DD)
    new: int             # accounts created that day
    cumulative: int      # running total through that day


class AdminSignupsOut(BaseModel):
    total: int
    days: list[SignupDay]   # one contiguous entry per day from first signup to today (gaps = 0)


# --------------------------------------------------------------------------- ask (in-app NL query)
class AskIn(BaseModel):
    question: str
    season: int | None = None


class AskMessageOut(ORMModel):
    role: str  # "user" | "assistant"
    content: str
    tools: list[str] | None = None


class AskOut(BaseModel):
    answer: str
    tools_used: list[str] = []
