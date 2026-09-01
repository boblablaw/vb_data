"""Read-only stat query tools, exposed to LLMs as callable tools.

One registry (``TOOL_SPECS``) + one dispatcher (``run_tool``) drives both front-doors:
  * the MCP server (``vb.mcp.server``) turns each spec into an MCP tool, and
  * the in-app Ask box (``vb.api.routers.ask``) hands the specs to Claude as tool definitions.

Every tool is a plain function that takes a SQLAlchemy Session and returns JSON-serializable data;
none of them mutate. Filters (``class_year``, ``position``, ``conference``) make natural-language
questions like *"freshmen with the most kills so far"* answerable.
"""
from __future__ import annotations

from sqlalchemy import desc, nulls_last, or_, select
from sqlalchemy.orm import Session

from ..api.routers.stats import compute_team_records
from ..models import (
    Conference,
    Contest,
    Player,
    PlayerGameStat,
    PlayerSeasonStat,
    Team,
)
from ..util import current_season, normalize_class

_RANKABLE = {
    "kills", "errors", "total_attacks", "assists", "aces", "serr", "digs", "retatt", "rerr",
    "block_solos", "block_assists", "total_blocks", "berr", "pts", "bhe", "hit_pct",
    "kills_per_set", "assists_per_set", "aces_per_set", "digs_per_set", "blocks_per_set",
    "pts_per_set",
}
_MAX_LIMIT = 100


def _season(season: int | None) -> int:
    return season if season is not None else current_season()


def _class_clause(class_year: str):
    """Flexible class-year match: 'freshman'/'Fr'/'fr' all match stored 'Fr' and 'R-Fr'."""
    code = normalize_class(class_year)
    base = (code[-2:] if code else class_year).strip()
    if not base:
        return None
    return or_(Player.class_year.ilike(f"%{base}%"), Player.class_year.ilike(f"%{class_year}%"))


def leaderboard(
    db: Session, *, stat: str = "kills", season: int | None = None,
    class_year: str | None = None, position: str | None = None,
    conference: str | None = None, min_sets: float = 0, limit: int = 25,
) -> list[dict]:
    """Top players for a season by a stat, with optional class/position/conference filters."""
    if stat not in _RANKABLE:
        return {"error": f"unknown stat '{stat}'. Valid: {sorted(_RANKABLE)}"}
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    msv = PlayerSeasonStat
    value = getattr(msv, stat)
    stmt = (
        select(
            Player.name, Player.position, Player.class_year,
            Team.name.label("team"), Conference.name.label("conference"),
            msv.gp.label("games"), msv.sp.label("sets"), value.label("value"),
        )
        .select_from(msv)
        .join(Player, Player.id == msv.player_id)
        .join(Team, Team.id == Player.team_id, isouter=True)
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .where(msv.season == season)
    )
    if class_year:
        clause = _class_clause(class_year)
        if clause is not None:
            stmt = stmt.where(clause)
    if position:
        stmt = stmt.where(Player.position.ilike(f"%{position}%"))
    if conference:
        stmt = stmt.where(Conference.name.ilike(f"%{conference}%"))
    if min_sets:
        stmt = stmt.where(msv.sp >= float(min_sets))
    stmt = stmt.order_by(nulls_last(desc(value))).limit(limit)
    return [
        {
            "rank": i + 1, "player": r.name, "team": r.team, "conference": r.conference,
            "class_year": r.class_year, "position": r.position,
            "games": int(r.games) if r.games is not None else None,
            "sets": float(r.sets) if r.sets is not None else None,
            "stat": stat, "value": float(r.value) if r.value is not None else None,
        }
        for i, r in enumerate(db.execute(stmt).all())
    ]


def search_players(db: Session, *, query: str, season: int | None = None, limit: int = 20) -> list[dict]:
    """Find players (and their team) by a name substring for the current/given season."""
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    rows = db.execute(
        select(Player.id, Player.name, Player.position, Player.class_year, Team.name.label("team"))
        .join(Team, Team.id == Player.team_id, isouter=True)
        .where(Player.season == season, Player.name.ilike(f"%{query}%"))
        .order_by(Player.name).limit(limit)
    ).all()
    return [
        {"player_id": r.id, "player": r.name, "team": r.team,
         "position": r.position, "class_year": r.class_year}
        for r in rows
    ]


def team_records(db: Session, *, season: int | None = None, conference: str | None = None) -> list[dict]:
    """Team season records (W-L, sets, conference splits, streak) derived from match linescores."""
    season = _season(season)
    teams = {
        r.id: {
            "name": r.name, "team_short": r.short_name, "conference": r.conference,
            "conference_id": r.conference_id, "rpi_rank": r.rpi_rank, "rpi_record": r.rpi_record,
        }
        for r in db.execute(
            select(
                Team.id, Team.name, Team.short_name, Conference.name.label("conference"),
                Team.conference_id, Team.rpi_rank, Team.rpi_record,
            ).join(Conference, Conference.id == Team.conference_id, isouter=True)
        ).all()
    }
    contests = [
        {"date": c.date, "home_team_id": c.home_team_id, "away_team_id": c.away_team_id,
         "home_sets_won": c.home_sets_won, "away_sets_won": c.away_sets_won}
        for c in db.execute(
            select(Contest.date, Contest.home_team_id, Contest.away_team_id,
                   Contest.home_sets_won, Contest.away_sets_won).where(Contest.season == season)
        ).all()
    ]
    records = compute_team_records(contests, teams)
    if conference:
        records = [r for r in records if r["conference"] and conference.lower() in r["conference"].lower()]
    records.sort(key=lambda r: (-r["wins"], r["losses"]))
    # Trim to the fields useful in an NL answer.
    return [
        {k: r[k] for k in (
            "team", "conference", "wins", "losses", "sets_won", "sets_lost",
            "conf_wins", "conf_losses", "win_streak", "rpi_rank",
        )}
        for r in records
    ]


def player_game_log(db: Session, *, player_id: int, season: int | None = None) -> list[dict]:
    """A single player's per-game stat lines with opponent + date."""
    stmt = (
        select(PlayerGameStat, Contest.date, Contest.home_team_id, Contest.away_team_id)
        .join(Contest, Contest.contest_id == PlayerGameStat.contest_id, isouter=True)
        .where(PlayerGameStat.player_id == player_id)
    )
    if season is not None:
        stmt = stmt.where(PlayerGameStat.season == season)
    rows = db.execute(stmt.order_by(nulls_last(Contest.date.asc()))).all()
    opp_ids = {
        (home if pgs.team_id == away else away)
        for pgs, _d, home, away in rows if (home or away)
    }
    names = {
        tid: nm for tid, nm in db.execute(
            select(Team.id, Team.name).where(Team.id.in_(opp_ids or {-1}))
        ).all()
    }
    out = []
    for pgs, date_str, home, away in rows:
        opp = home if pgs.team_id == away else away
        out.append({
            "date": date_str, "opponent": names.get(opp),
            "sets": pgs.sets, "kills": pgs.kills, "errors": pgs.errors,
            "total_attacks": pgs.total_attacks, "assists": pgs.assists, "aces": pgs.aces,
            "digs": pgs.digs, "block_solos": pgs.block_solos, "block_assists": pgs.block_assists,
            "pts": pgs.pts,
        })
    return out


# --------------------------------------------------------------------------- tool registry
# JSON-schema tool specs shared by the MCP server and the Ask box (Anthropic tool-use format).
TOOL_SPECS: list[dict] = [
    {
        "name": "leaderboard",
        "description": (
            "Rank the top players for a season by a counting or per-set stat, with optional "
            "filters. Use this for questions like 'who leads in kills', 'freshmen with the most "
            "kills', 'best passers in the Big Ten'. class_year accepts 'freshman'/'Fr', "
            "'sophomore'/'So', 'junior'/'Jr', 'senior'/'Sr', 'graduate'/'Gr'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stat": {"type": "string", "description": f"one of {sorted(_RANKABLE)}"},
                "season": {"type": "integer", "description": "fall year, e.g. 2026; omit for current"},
                "class_year": {"type": "string"},
                "position": {"type": "string", "description": "e.g. OH, MB, S, L, DS, OPP"},
                "conference": {"type": "string"},
                "min_sets": {"type": "number", "description": "minimum sets played (rate qualifier)"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "search_players",
        "description": "Find players by a name substring (returns player_id, team, position, class).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "season": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "team_records",
        "description": "Team season win/loss records, set records, conference splits, and streaks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
            },
        },
    },
    {
        "name": "player_game_log",
        "description": "A single player's per-match stat lines (needs player_id from search_players).",
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "integer"},
                "season": {"type": "integer"},
            },
            "required": ["player_id"],
        },
    },
]

_DISPATCH = {
    "leaderboard": leaderboard,
    "search_players": search_players,
    "team_records": team_records,
    "player_game_log": player_game_log,
}


def run_tool(db: Session, name: str, args: dict) -> object:
    """Dispatch a tool call by name with keyword args. Returns JSON-serializable data."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'"}
    try:
        return fn(db, **(args or {}))
    except TypeError as e:
        return {"error": f"bad arguments for '{name}': {e}"}
    except Exception as e:
        return {"error": f"tool '{name}' failed: {e}"}
