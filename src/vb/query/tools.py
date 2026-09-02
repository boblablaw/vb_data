"""Read-only stat query tools, exposed to LLMs as callable tools.

One registry (``TOOL_SPECS``) + one dispatcher (``run_tool``) drives both front-doors:
  * the MCP server (``vb.mcp.server``) turns each spec into an MCP tool, and
  * the in-app Ask box (``vb.api.routers.ask``) hands the specs to Claude as tool definitions.

Every tool is a plain function that takes a SQLAlchemy Session and returns JSON-serializable data;
none of them mutate. Filters (``class_year``, ``position``, ``conference``) make natural-language
questions like *"freshmen with the most kills so far"* answerable.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import timedelta

from sqlalchemy import Text, and_, case, cast, desc, func, not_, nulls_last, or_, select
from sqlalchemy.orm import Session

from ..api.routers.stats import compute_team_records
from ..models import (
    Conference,
    Contest,
    Player,
    PlayerGameStat,
    PlayerSeasonStat,
    Schedule,
    Team,
)
from ..util import current_season, normalize_class, normalize_school_key

_RANKABLE = {
    "kills", "errors", "total_attacks", "assists", "aces", "serr", "digs", "retatt", "rerr",
    "block_solos", "block_assists", "total_blocks", "berr", "pts", "bhe", "hit_pct",
    "kills_per_set", "assists_per_set", "aces_per_set", "digs_per_set", "blocks_per_set",
    "pts_per_set",
}
_MAX_LIMIT = 100

# Player hometowns are stored as free text, US rows as "City, ST". Map full state names to the
# postal abbreviation so "from Indiana" matches "Indianapolis, IN". Two-letter inputs pass through.
US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


# Non-state territory codes that also appear as US hometown tails; treated as domestic (not
# "international"). Puerto Rico shows up both spelled out and as "PR".
US_TERRITORIES = {"PR", "VI", "GU", "AS", "MP"}
_US_TAIL_CODES = sorted(set(US_STATES.values()) | US_TERRITORIES)

# Country spellings a user might type → the name actually stored in hometowns. "__us__" is a
# sentinel meaning "any domestic hometown" (used by _country_clause for USA/US/etc.).
_COUNTRY_ALIASES = {
    "usa": "__us__", "us": "__us__", "u.s.": "__us__", "u.s.a.": "__us__",
    "united states": "__us__", "united states of america": "__us__", "america": "__us__",
    "uk": "United Kingdom", "great britain": "United Kingdom", "england": "United Kingdom",
    "czech republic": "Czechia", "holland": "Netherlands",
}


def _domestic_clauses() -> list:
    """OR-clauses matching a US/territory hometown ("City, ST" or spelled-out Puerto Rico)."""
    return [Player.hometown.ilike(f"%, {code}") for code in _US_TAIL_CODES] + [
        Player.hometown.ilike("%, Puerto Rico"),
    ]


def _country_clause(country: str):
    """Match players whose hometown is in ``country``. 'USA' (and aliases) matches all domestic
    players; otherwise match the trailing ", <Country>" of the free-text hometown."""
    c = (country or "").strip()
    if not c:
        return None
    canon = _COUNTRY_ALIASES.get(c.lower(), c)
    if canon == "__us__":
        return or_(*_domestic_clauses())
    if canon.lower() in ("puerto rico", "pr"):
        return or_(Player.hometown.ilike("%, Puerto Rico"), Player.hometown.ilike("%, PR"))
    return Player.hometown.ilike(f"%, {canon}")


def _international_clause():
    """Match players with a foreign hometown — one that doesn't end in a US state/territory tail."""
    return and_(Player.hometown.is_not(None), not_(or_(*_domestic_clauses())))


def _season(season: int | None) -> int:
    return season if season is not None else current_season()


def _state_clause(state: str):
    """Match players whose hometown ends in the given US state (full name or 2-letter code)."""
    s = (state or "").strip()
    abbr = US_STATES.get(s.lower()) or (s.upper() if len(s) == 2 else None)
    if not abbr:
        return None
    # Hometowns look like "Indianapolis, IN" — match on the trailing ", ST".
    return Player.hometown.ilike(f"%, {abbr}")


# Spelled-out position words → the stored code, so "setter" and "S" both work.
_POSITION_WORDS = {
    "setter": "S", "outside": "OH", "outside hitter": "OH", "pin": "OH",
    "middle": "MB", "middle blocker": "MB", "libero": "L",
    "opposite": "OPP", "right side": "RS", "rightside": "RS", "defensive specialist": "DS",
}


def _position_clause(position: str):
    """Whole-token position match so "S" (setter) doesn't also catch "DS"/"L/DS".

    Positions are stored as slash-delimited codes (e.g. "S", "OH/RS", "L/DS"); match the requested
    code only as a full token within that string."""
    p = (position or "").strip()
    if not p:
        return None
    code = _POSITION_WORDS.get(p.lower(), p).upper()
    return or_(
        Player.position.ilike(code),
        Player.position.ilike(f"{code}/%"),
        Player.position.ilike(f"%/{code}"),
        Player.position.ilike(f"%/{code}/%"),
    )


def _resolve_team_id(db: Session, team: str) -> int | None:
    """Resolve a team name/short_name/alias (fuzzy) to a team id; None if no confident match."""
    if not team:
        return None
    key = normalize_school_key(team)
    lookup: dict[str, int] = {}
    for t in db.scalars(select(Team)).all():
        lookup.setdefault(normalize_school_key(t.name), t.id)
        if t.short_name:
            lookup.setdefault(normalize_school_key(t.short_name), t.id)
        for a in (t.aliases or []):
            lookup.setdefault(normalize_school_key(a), t.id)
    if key in lookup:
        return lookup[key]
    # Fall back to a substring match on the raw name/short_name.
    row = db.execute(
        select(Team.id).where(
            or_(Team.name.ilike(f"%{team}%"), Team.short_name.ilike(f"%{team}%"))
        ).limit(1)
    ).first()
    return row[0] if row else None


def _conference_clause(conference: str):
    """Match a conference by full name OR short name/abbreviation (e.g. 'MAC', 'Big Ten')."""
    c = (conference or "").strip()
    if not c:
        return None
    return or_(Conference.name.ilike(f"%{c}%"), Conference.short_name.ilike(f"%{c}%"))


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
    conference: str | None = None, team: str | None = None,
    state: str | None = None, hometown: str | None = None,
    country: str | None = None, international: bool = False,
    min_sets: float = 0, limit: int = 25,
) -> list[dict]:
    """Top players for a season by a stat, with optional class/position/conference/hometown filters."""
    if stat not in _RANKABLE:
        return {"error": f"unknown stat '{stat}'. Valid: {sorted(_RANKABLE)}"}
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    msv = PlayerSeasonStat
    value = getattr(msv, stat)
    stmt = (
        select(
            Player.name, Player.position, Player.class_year, Player.hometown, Player.high_school,
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
        clause = _position_clause(position)
        if clause is not None:
            stmt = stmt.where(clause)
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    if team:
        tid = _resolve_team_id(db, team)
        if tid is None:
            return {"error": f"no team matched '{team}'"}
        stmt = stmt.where(Player.team_id == tid)
    if state:
        clause = _state_clause(state)
        if clause is not None:
            stmt = stmt.where(clause)
    if hometown:
        stmt = stmt.where(Player.hometown.ilike(f"%{hometown}%"))
    if country:
        clause = _country_clause(country)
        if clause is not None:
            stmt = stmt.where(clause)
    if international:
        stmt = stmt.where(_international_clause())
    if min_sets:
        stmt = stmt.where(msv.sp >= float(min_sets))
    stmt = stmt.order_by(nulls_last(desc(value))).limit(limit)
    return [
        {
            "rank": i + 1, "player": r.name, "team": r.team, "conference": r.conference,
            "class_year": r.class_year, "position": r.position,
            "hometown": r.hometown, "high_school": r.high_school,
            "games": int(r.games) if r.games is not None else None,
            "sets": float(r.sets) if r.sets is not None else None,
            "stat": stat, "value": float(r.value) if r.value is not None else None,
        }
        for i, r in enumerate(db.execute(stmt).all())
    ]


def search_players(
    db: Session, *, query: str | None = None, season: int | None = None,
    position: str | None = None, class_year: str | None = None,
    conference: str | None = None, team: str | None = None,
    state: str | None = None, hometown: str | None = None,
    country: str | None = None, international: bool = False,
    min_height_inches: int | None = None, max_height_inches: int | None = None,
    sort_by: str = "name", limit: int = 20,
) -> list[dict]:
    """Find players by name and/or roster attributes (team, hometown, state, position, class,
    conference), with optional height filters/sorting.

    Returns each player's team plus roster bio (hometown, high school, height, jersey number). At
    least one filter should be given; with none, returns an alphabetical slice of the season.
    ``sort_by='height'`` ranks tallest-first (use with position/conference for 'tallest liberos' or
    'tallest players in D1'); ``min_height_inches`` / ``max_height_inches`` filter by height
    (convert feet-inches to inches, e.g. 6-6 = 78)."""
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    stmt = (
        select(
            Player.id, Player.name, Player.position, Player.class_year, Player.number,
            Player.height_inches, Player.hometown, Player.high_school,
            Team.name.label("team"), Conference.name.label("conference"),
        )
        .join(Team, Team.id == Player.team_id, isouter=True)
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .where(Player.season == season)
    )
    if query:
        stmt = stmt.where(Player.name.ilike(f"%{query}%"))
    if position:
        clause = _position_clause(position)
        if clause is not None:
            stmt = stmt.where(clause)
    if class_year:
        clause = _class_clause(class_year)
        if clause is not None:
            stmt = stmt.where(clause)
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    if team:
        tid = _resolve_team_id(db, team)
        if tid is None:
            return {"error": f"no team matched '{team}'"}
        stmt = stmt.where(Player.team_id == tid)
    if state:
        clause = _state_clause(state)
        if clause is not None:
            stmt = stmt.where(clause)
    if hometown:
        stmt = stmt.where(Player.hometown.ilike(f"%{hometown}%"))
    if country:
        clause = _country_clause(country)
        if clause is not None:
            stmt = stmt.where(clause)
    if international:
        stmt = stmt.where(_international_clause())
    if min_height_inches is not None:
        stmt = stmt.where(Player.height_inches >= int(min_height_inches))
    if max_height_inches is not None:
        stmt = stmt.where(Player.height_inches <= int(max_height_inches))
    if sort_by == "height":  # tallest first; players with no recorded height sort last
        stmt = stmt.order_by(nulls_last(desc(Player.height_inches)), Player.name)
    else:
        stmt = stmt.order_by(Player.name)
    rows = db.execute(stmt.limit(limit)).all()
    return [
        {"player_id": r.id, "player": r.name, "team": r.team, "conference": r.conference,
         "position": r.position, "class_year": r.class_year, "number": r.number,
         "height_inches": r.height_inches, "height": _height_str(r.height_inches),
         "hometown": r.hometown, "high_school": r.high_school}
        for r in rows
    ]


def team_records(db: Session, *, season: int | None = None, conference: str | None = None) -> list[dict]:
    """Team season records (W-L, sets, conference splits, streak) derived from match linescores."""
    season = _season(season)
    teams = {
        r.id: {
            "name": r.name, "team_short": r.short_name, "conference": r.conference,
            "conference_id": r.conference_id, "rpi_rank": r.rpi_rank, "rpi_record": r.rpi_record,
            "avca_rank": r.avca_rank,
        }
        for r in db.execute(
            select(
                Team.id, Team.name, Team.short_name, Conference.name.label("conference"),
                Team.conference_id, Team.rpi_rank, Team.rpi_record, Team.avca_rank,
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
        # Resolve the input (full name or abbreviation like 'MAC') to the matching conference names,
        # then keep records in any of them.
        names = {
            n for (n,) in db.execute(
                select(Conference.name).where(_conference_clause(conference))
            ).all() if n
        }
        records = [r for r in records if r["conference"] in names]
    records.sort(key=lambda r: (-r["wins"], r["losses"]))
    # Trim to the fields useful in an NL answer.
    return [
        {k: r[k] for k in (
            "team", "conference", "wins", "losses", "sets_won", "sets_lost",
            "conf_wins", "conf_losses", "win_streak", "rpi_rank", "avca_rank",
        )}
        for r in records
    ]


def list_teams(
    db: Session, *, query: str | None = None, conference: str | None = None, limit: int = 50,
) -> list[dict]:
    """List/search team identities (name, short name, conference) to ground fuzzy name matching.

    Use this to confirm a school's exact name before another tool, or to resolve an abbreviation or
    nickname you're unsure of: pass a substring (matches name/short name/alias) and/or a conference.
    Returns every team when given no filters (capped by ``limit``)."""
    limit = max(1, min(int(limit), _MAX_LIMIT))
    stmt = (
        select(Team.name, Team.short_name, Team.aliases, Conference.name.label("conference"))
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .order_by(Team.name)
    )
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    if query:
        q = f"%{query}%"
        # Match a substring of the name, short name, or any stored alias (aliases is jsonb → cast to
        # text so a plain ILIKE can scan the serialized list).
        stmt = stmt.where(or_(
            Team.name.ilike(q), Team.short_name.ilike(q),
            cast(Team.aliases, Text).ilike(q),
        ))
    rows = db.execute(stmt.limit(limit)).all()
    return [
        {"team": r.name, "short_name": r.short_name, "conference": r.conference,
         "aliases": list(r.aliases or [])}
        for r in rows
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


_SEASON_STAT_FIELDS = (
    "gp", "sp", "kills", "errors", "total_attacks", "hit_pct", "assists", "aces", "serr",
    "digs", "retatt", "rerr", "block_solos", "block_assists", "total_blocks", "berr", "pts", "bhe",
    "kills_per_set", "assists_per_set", "aces_per_set", "digs_per_set", "blocks_per_set",
    "pts_per_set",
)


def player_stats(db: Session, *, player_id: int, season: int | None = None) -> dict:
    """A single player's full season-to-date stat totals + per-set rates and bio.

    Use after ``search_players`` gives a player_id — this returns the season line directly rather
    than requiring the player to place on a leaderboard."""
    season = _season(season)
    row = db.execute(
        select(Player, PlayerSeasonStat, Team.name.label("team"))
        .join(PlayerSeasonStat, (PlayerSeasonStat.player_id == Player.id)
              & (PlayerSeasonStat.season == Player.season), isouter=True)
        .join(Team, Team.id == Player.team_id, isouter=True)
        .where(Player.id == player_id, Player.season == season)
    ).first()
    if row is None:
        return {"error": f"no player {player_id} in season {season}"}
    p, ss, team = row
    out = {
        "player_id": p.id, "player": p.name, "team": team, "season": season,
        "position": p.position, "class_year": p.class_year, "number": p.number,
        "height_inches": p.height_inches, "hometown": p.hometown, "high_school": p.high_school,
    }
    for f in _SEASON_STAT_FIELDS:
        v = getattr(ss, f, None) if ss is not None else None
        out[f] = float(v) if v is not None else None
    return out


_TEAM_AGG = {
    "kills", "assists", "aces", "digs", "total_blocks", "pts", "errors", "total_attacks", "hit_pct",
}


def team_stats(
    db: Session, *, season: int | None = None, conference: str | None = None,
    sort_by: str = "kills", limit: int = 25,
) -> list[dict]:
    """Team-aggregate season stats (summed over the roster's game stats), ranked by ``sort_by``.

    ``sort_by`` is one of kills, assists, aces, digs, total_blocks, pts, hit_pct. Use for questions
    like 'which team has the most kills' or 'best hitting team'."""
    if sort_by not in _TEAM_AGG:
        return {"error": f"unknown sort_by '{sort_by}'. Valid: {sorted(_TEAM_AGG)}"}
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    pgs = PlayerGameStat
    kills, errors, ta = func.sum(pgs.kills), func.sum(pgs.errors), func.sum(pgs.total_attacks)
    hit_pct = func.nullif(ta, 0)
    stmt = (
        select(
            Team.name.label("team"), Conference.name.label("conference"),
            func.count(func.distinct(pgs.contest_id)).label("games"),
            kills.label("kills"), func.sum(pgs.assists).label("assists"),
            func.sum(pgs.aces).label("aces"), func.sum(pgs.digs).label("digs"),
            (func.sum(pgs.block_solos) + func.sum(pgs.block_assists)).label("total_blocks"),
            func.sum(pgs.pts).label("pts"), errors.label("errors"), ta.label("total_attacks"),
            ((kills - errors) / hit_pct).label("hit_pct"),
        )
        .select_from(pgs)
        .join(Team, Team.id == pgs.team_id)
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .where(pgs.season == season)
        .group_by(Team.name, Conference.name)
    )
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    order = {
        "kills": kills, "assists": func.sum(pgs.assists), "aces": func.sum(pgs.aces),
        "digs": func.sum(pgs.digs),
        "total_blocks": func.sum(pgs.block_solos) + func.sum(pgs.block_assists),
        "pts": func.sum(pgs.pts), "hit_pct": (kills - errors) / hit_pct,
    }[sort_by]
    stmt = stmt.order_by(nulls_last(desc(order))).limit(limit)
    return [
        {
            "team": r.team, "conference": r.conference,
            "games": int(r.games) if r.games is not None else None,
            "kills": float(r.kills) if r.kills is not None else None,
            "assists": float(r.assists) if r.assists is not None else None,
            "aces": float(r.aces) if r.aces is not None else None,
            "digs": float(r.digs) if r.digs is not None else None,
            "total_blocks": float(r.total_blocks) if r.total_blocks is not None else None,
            "pts": float(r.pts) if r.pts is not None else None,
            "hit_pct": round(float(r.hit_pct), 3) if r.hit_pct is not None else None,
        }
        for r in db.execute(stmt).all()
    ]


def _height_str(inches) -> str | None:
    """Inches → feet-inches display like 6-2; None passes through."""
    if inches is None:
        return None
    ft, inch = divmod(round(float(inches)), 12)
    return f"{ft}-{inch}"


def team_heights(
    db: Session, *, season: int | None = None, conference: str | None = None,
    position: str | None = None, sort_by: str = "avg_height", limit: int = 25,
) -> list[dict]:
    """Per-team roster height, ranked — average height and tallest player on each roster.

    Use for 'tallest team', 'shortest team' (sort_by=avg_height, read from the bottom), 'which team
    is biggest', or 'team with the tallest player' (sort_by=max_height). Optional ``conference`` and
    ``position`` filters — e.g. position='MB' answers 'which team has the tallest middles'. Only
    players with a recorded height count; ``players_measured`` shows the sample size so a team with
    very few measured players can be discounted."""
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    avg_h = func.avg(Player.height_inches)
    max_h = func.max(Player.height_inches)
    order = {"avg_height": avg_h, "max_height": max_h}.get(sort_by)
    if order is None:
        return {"error": f"unknown sort_by '{sort_by}'. Valid: ['avg_height', 'max_height']"}
    stmt = (
        select(
            Team.name.label("team"), Conference.name.label("conference"),
            func.count(Player.height_inches).label("players_measured"),
            avg_h.label("avg_height"), max_h.label("max_height"),
        )
        .select_from(Player)
        .join(Team, Team.id == Player.team_id)
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .where(Player.season == season, Player.height_inches.is_not(None))
        .group_by(Team.name, Conference.name)
    )
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    if position:
        clause = _position_clause(position)
        if clause is not None:
            stmt = stmt.where(clause)
    stmt = stmt.order_by(nulls_last(desc(order))).limit(limit)
    return [
        {
            "team": r.team, "conference": r.conference,
            "players_measured": int(r.players_measured),
            "avg_height_inches": round(float(r.avg_height), 1) if r.avg_height is not None else None,
            "avg_height": _height_str(r.avg_height),
            "tallest_inches": int(r.max_height) if r.max_height is not None else None,
            "tallest": _height_str(r.max_height),
        }
        for r in db.execute(stmt).all()
    ]


_GAME_STATS = {
    "kills", "assists", "digs", "aces", "total_blocks", "pts", "errors", "total_attacks",
}


def _game_value(pgs, stat):
    """Per-match column expression for a game stat (total_blocks is derived)."""
    if stat == "total_blocks":
        return func.coalesce(pgs.block_solos, 0) + func.coalesce(pgs.block_assists, 0)
    return getattr(pgs, stat)


def game_highs(
    db: Session, *, stat: str = "kills", season: int | None = None,
    conference: str | None = None, team: str | None = None, position: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """Best single-MATCH individual performances, ranked (not season totals).

    Use for 'most kills in a single match', 'best single-game performance', 'highest single-match
    dig total'. stat is one of kills|assists|digs|aces|total_blocks|pts. Optional conference/team/
    position filters. Returns the player, opponent, date, and the stat value for each top game."""
    if stat not in _GAME_STATS:
        return {"error": f"unknown stat '{stat}'. Valid: {sorted(_GAME_STATS)}"}
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    pgs = PlayerGameStat
    value = _game_value(pgs, stat)
    stmt = (
        select(
            Player.name.label("player"), Player.position, Team.name.label("team"),
            Conference.name.label("conference"), pgs.team_id, Contest.date,
            Contest.home_team_id, Contest.away_team_id, pgs.sets, value.label("value"),
        )
        .select_from(pgs)
        .join(Player, Player.id == pgs.player_id)
        .join(Team, Team.id == Player.team_id, isouter=True)
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .join(Contest, Contest.contest_id == pgs.contest_id, isouter=True)
        .where(pgs.season == season, value.is_not(None))
    )
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    if team:
        tid = _resolve_team_id(db, team)
        if tid is None:
            return {"error": f"no team matched '{team}'"}
        stmt = stmt.where(Player.team_id == tid)
    if position:
        clause = _position_clause(position)
        if clause is not None:
            stmt = stmt.where(clause)
    stmt = stmt.order_by(nulls_last(desc(value))).limit(limit)
    rows = db.execute(stmt).all()
    opp_ids = {
        (r.home_team_id if r.team_id == r.away_team_id else r.away_team_id)
        for r in rows if (r.home_team_id or r.away_team_id)
    }
    names = {
        tid: nm for tid, nm in db.execute(
            select(Team.id, Team.name).where(Team.id.in_(opp_ids or {-1}))
        ).all()
    }
    out = []
    for i, r in enumerate(rows):
        opp = r.home_team_id if r.team_id == r.away_team_id else r.away_team_id
        out.append({
            "rank": i + 1, "player": r.player, "team": r.team, "conference": r.conference,
            "position": r.position, "opponent": names.get(opp),
            "date": r.date[:10] if r.date else None, "sets": r.sets,
            "stat": stat, "value": float(r.value) if r.value is not None else None,
        })
    return out


def double_doubles(
    db: Session, *, season: int | None = None, conference: str | None = None,
    team: str | None = None, limit: int = 25,
) -> list[dict]:
    """Players ranked by number of double-doubles (and triple-doubles) this season.

    A double-double is a match with >=10 in at least two of: kills, assists, digs, aces, total
    blocks; a triple-double is >=3 such categories. Use for 'who has the most double-doubles',
    'any triple-doubles this year'. Optional conference/team filters."""
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    pgs = PlayerGameStat
    cats = [
        func.coalesce(pgs.kills, 0), func.coalesce(pgs.assists, 0), func.coalesce(pgs.digs, 0),
        func.coalesce(pgs.aces, 0),
        func.coalesce(pgs.block_solos, 0) + func.coalesce(pgs.block_assists, 0),
    ]
    n_expr = None
    for c in cats:
        term = case((c >= 10, 1), else_=0)
        n_expr = term if n_expr is None else n_expr + term
    sub = (
        select(pgs.player_id.label("pid"), n_expr.label("n"))
        .select_from(pgs)
        .join(Player, Player.id == pgs.player_id)
        .join(Team, Team.id == Player.team_id, isouter=True)
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .where(pgs.season == season)
    )
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            sub = sub.where(clause)
    if team:
        tid = _resolve_team_id(db, team)
        if tid is None:
            return {"error": f"no team matched '{team}'"}
        sub = sub.where(Player.team_id == tid)
    sub = sub.subquery()
    dd = func.sum(case((sub.c.n >= 2, 1), else_=0))
    td = func.sum(case((sub.c.n >= 3, 1), else_=0))
    stmt = (
        select(
            Player.name.label("player"), Player.position, Team.name.label("team"),
            Conference.name.label("conference"), dd.label("dd"), td.label("td"),
        )
        .select_from(sub)
        .join(Player, Player.id == sub.c.pid)
        .join(Team, Team.id == Player.team_id, isouter=True)
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .group_by(Player.name, Player.position, Team.name, Conference.name)
        .having(dd > 0)
        .order_by(desc(dd), desc(td))
        .limit(limit)
    )
    return [
        {
            "rank": i + 1, "player": r.player, "team": r.team, "conference": r.conference,
            "position": r.position, "double_doubles": int(r.dd), "triple_doubles": int(r.td),
        }
        for i, r in enumerate(db.execute(stmt).all())
    ]


# Class-year → ordinal (younger = smaller), for roster-age aggregates.
_CLASS_ORDINAL = {"Fr": 1, "So": 2, "Jr": 3, "Sr": 4, "Gr": 5}
_US_TAIL_SET = set(_US_TAIL_CODES)


def _is_international(hometown: str | None) -> bool:
    """True when a hometown is foreign (doesn't end in a US state/territory tail). Mirrors
    ``_international_clause`` for Python-side aggregation."""
    if not hometown:
        return False
    ht = hometown.strip()
    if ht.lower().endswith(", puerto rico"):
        return False
    tail = ht.rsplit(", ", 1)[-1].strip().upper() if ", " in ht else ""
    return tail not in _US_TAIL_SET


def _class_ordinal(class_year: str | None) -> int | None:
    code = normalize_class(class_year) if class_year else None
    base = (code[-2:] if code else "").strip()
    return _CLASS_ORDINAL.get(base)


def team_roster_makeup(
    db: Session, *, season: int | None = None, conference: str | None = None,
    sort_by: str = "international", limit: int = 25,
) -> list[dict]:
    """Per-team roster demographics, ranked: size, international count/%, and average class year.

    Use for 'which team has the most international players', 'youngest/oldest team', 'biggest
    roster'. sort_by: international (count, default) | international_pct | youngest | oldest | size.
    avg_class_ordinal is 1=Fr..5=Gr (lower = younger). Optional conference filter."""
    if sort_by not in {"international", "international_pct", "youngest", "oldest", "size"}:
        return {"error": "sort_by must be international|international_pct|youngest|oldest|size"}
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    stmt = (
        select(
            Team.name.label("team"), Conference.name.label("conference"),
            Player.class_year, Player.hometown,
        )
        .select_from(Player)
        .join(Team, Team.id == Player.team_id)
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .where(Player.season == season)
    )
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    agg: dict[str, dict] = {}
    for r in db.execute(stmt).all():
        a = agg.setdefault(r.team, {"conference": r.conference, "size": 0, "intl": 0, "ords": []})
        a["size"] += 1
        if _is_international(r.hometown):
            a["intl"] += 1
        o = _class_ordinal(r.class_year)
        if o is not None:
            a["ords"].append(o)
    out = []
    for team, a in agg.items():
        avg_class = round(sum(a["ords"]) / len(a["ords"]), 2) if a["ords"] else None
        out.append({
            "team": team, "conference": a["conference"], "roster_size": a["size"],
            "international": a["intl"],
            "international_pct": round(100 * a["intl"] / a["size"], 1) if a["size"] else None,
            "avg_class_ordinal": avg_class,
        })
    keys = {
        "international": lambda x: (-x["international"], -(x["international_pct"] or 0)),
        "international_pct": lambda x: (-(x["international_pct"] or 0), -x["international"]),
        "size": lambda x: -x["roster_size"],
        "youngest": lambda x: (x["avg_class_ordinal"] is None, x["avg_class_ordinal"] or 0),
        "oldest": lambda x: (x["avg_class_ordinal"] is None, -(x["avg_class_ordinal"] or 0)),
    }
    out.sort(key=keys[sort_by])
    return out[:limit]


def player_origins(
    db: Session, *, group_by: str = "state", season: int | None = None,
    conference: str | None = None, limit: int = 25,
) -> list[dict]:
    """Where players come from, grouped and counted. group_by='state' (US home state) or 'country'.

    Use for 'which state sends the most players' (optionally to a conference), 'how many countries
    are represented', 'most common home state in the Big Ten'. Optional conference filter."""
    if group_by not in {"state", "country"}:
        return {"error": "group_by must be 'state' or 'country'"}
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    stmt = (
        select(Player.hometown)
        .select_from(Player)
        .join(Team, Team.id == Player.team_id)
        .join(Conference, Conference.id == Team.conference_id, isouter=True)
        .where(Player.season == season)
    )
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    counts: dict[str, int] = {}
    for (hometown,) in db.execute(stmt).all():
        if not hometown or ", " not in hometown:
            continue
        tail = hometown.rsplit(", ", 1)[-1].strip()
        if group_by == "state":
            if tail.upper() in _US_TAIL_SET:
                counts[tail.upper()] = counts.get(tail.upper(), 0) + 1
        elif _is_international(hometown):  # country
            counts[tail] = counts.get(tail, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    label = "state" if group_by == "state" else "country"
    return [{label: k, "players": v} for k, v in ranked]


def team_schedule(
    db: Session, *, team: str, season: int | None = None, upcoming_only: bool = False,
) -> dict:
    """A team's schedule: played results (from contests) + upcoming games (from the schedule table).

    ``team`` is a name/short name (e.g. "Nebraska"). Returns opponent, date, site, and result. Use
    for 'who does X play', 'X's next game', 'when does X play this week' (filter dates client-side)."""
    season = _season(season)
    tid = _resolve_team_id(db, team)
    if tid is None:
        return {"error": f"no team matching '{team}'"}
    team_name = db.scalar(select(Team.name).where(Team.id == tid))
    names = {t.id: t.name for t in db.scalars(select(Team)).all()}

    results = []
    if not upcoming_only:
        contests = db.scalars(
            select(Contest).where(
                Contest.season == season,
                or_(Contest.home_team_id == tid, Contest.away_team_id == tid),
            )
        ).all()
        for c in contests:
            is_home = c.home_team_id == tid
            opp_id = c.away_team_id if is_home else c.home_team_id
            tw = c.home_sets_won if is_home else c.away_sets_won
            ow = c.away_sets_won if is_home else c.home_sets_won
            res = None
            if tw is not None and ow is not None:
                res = f"{'W' if tw > ow else 'L'} {tw}-{ow}"
            results.append({
                "date": c.date, "site": "home" if is_home else "away",
                "opponent": names.get(opp_id), "result": res,
            })
        results.sort(key=lambda g: g["date"] or "")

    upcoming = []
    for s in db.scalars(
        select(Schedule).where(
            Schedule.season == season, Schedule.team_id == tid, Schedule.result_raw.is_(None),
        )
    ).all():
        upcoming.append({
            "date": s.date, "time": s.game_time, "site": s.site,
            "opponent": names.get(s.opponent_team_id) or s.opponent_name,
            "neutral_location": s.neutral_location,
        })
    upcoming.sort(key=lambda g: (g["date"] or "", g["time"] or ""))
    return {"team": team_name, "season": season, "results": results, "upcoming": upcoming}


def games_on_date(db: Session, *, date: str, season: int | None = None) -> list[dict]:
    """Every D1 game on a given date (YYYY-MM-DD): finals (from contests) + scheduled games.

    Use for 'what games are on <date>', 'who plays Friday' (resolve the weekday to a date first)."""
    season = _season(season)
    try:
        end_excl = (_date.fromisoformat(date) + timedelta(days=1)).isoformat()
    except ValueError:
        return {"error": f"bad date '{date}', expected YYYY-MM-DD"}
    names = {t.id: t.name for t in db.scalars(select(Team)).all()}

    out: list[dict] = []
    played_pairs: set[frozenset] = set()
    # contests.date carries a time suffix → exclusive upper bound.
    for c in db.scalars(
        select(Contest).where(
            Contest.season == season, Contest.date >= date, Contest.date < end_excl,
        )
    ).all():
        played_pairs.add(frozenset({c.home_team_id, c.away_team_id}))
        both = c.home_sets_won is not None and c.away_sets_won is not None
        out.append({
            "date": date, "status": "final",
            "away": names.get(c.away_team_id), "home": names.get(c.home_team_id),
            "score": f"{c.away_sets_won}-{c.home_sets_won}" if both else None,
        })

    seen: set = set()
    for s in db.scalars(
        select(Schedule).where(
            Schedule.season == season, Schedule.date == date, Schedule.result_raw.is_(None),
        )
    ).all():
        pair = frozenset(x for x in (s.team_id, s.opponent_team_id) if x)
        if s.opponent_team_id and pair in played_pairs:
            continue
        key = pair if s.opponent_team_id else (s.team_id, s.opponent_name)
        if key in seen:
            continue
        seen.add(key)
        opp = names.get(s.opponent_team_id) or s.opponent_name
        team_nm = names.get(s.team_id)
        away, home = (team_nm, opp) if s.site == "away" else (opp, team_nm)
        out.append({"date": date, "status": "scheduled", "time": s.game_time,
                    "away": away, "home": home})
    return out


# --------------------------------------------------------------------------- tool registry
# JSON-schema tool specs shared by the MCP server and the Ask box (Anthropic tool-use format).
TOOL_SPECS: list[dict] = [
    {
        "name": "leaderboard",
        "description": (
            "Rank the top players for a season by a counting or per-set stat, with optional "
            "filters. Use this for questions like 'who leads in kills', 'freshmen with the most "
            "kills', 'best passers in the Big Ten', 'sophomore setters from Indiana with the most "
            "assists', 'top international hitters', 'best players from Canada'. class_year accepts "
            "'freshman'/'Fr', 'sophomore'/'So', 'junior'/'Jr', 'senior'/'Sr', 'graduate'/'Gr'. "
            "'state' filters by the player's HOMETOWN state (full name like 'Indiana' or code 'IN') "
            "— use it for 'players from <state>'. 'hometown' matches any substring of the hometown "
            "(e.g. a city). 'country' filters by the player's home country (e.g. 'Canada', 'Serbia'; "
            "'USA' matches domestic players). 'international'=true limits to players from outside the "
            "US — use it for 'international players'. 'team' limits to one team's roster (name, short "
            "name, or alias) — use it for 'best hitter on Nebraska' or 'international players on <team>'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stat": {"type": "string", "description": f"one of {sorted(_RANKABLE)}"},
                "season": {"type": "integer", "description": "fall year, e.g. 2026; omit for current"},
                "class_year": {"type": "string"},
                "position": {"type": "string", "description": "e.g. OH, MB, S, L, DS, OPP"},
                "conference": {"type": "string"},
                "team": {"type": "string", "description": "team name/short name/alias, e.g. 'Nebraska'"},
                "state": {"type": "string", "description": "player's home state (name or 2-letter code)"},
                "hometown": {"type": "string", "description": "hometown substring, e.g. a city"},
                "country": {"type": "string", "description": "home country, e.g. 'Canada'; 'USA' = domestic"},
                "international": {"type": "boolean", "description": "true = only players from outside the US"},
                "min_sets": {"type": "number", "description": "minimum sets played (rate qualifier)"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "search_players",
        "description": (
            "Find players by name and/or roster attributes (hometown, home state, home country, "
            "position, class, conference). Returns player_id, team, position, class, jersey number, "
            "height, hometown, and high school. Use 'state' for 'players from <state>' (hometown "
            "state, full name or 2-letter code), 'hometown' for a city substring, 'country' for "
            "'players from <country>' (e.g. 'Canada'), and 'international'=true for 'all "
            "international players' (anyone from outside the US). 'team' limits to one team's roster "
            "(name, short name, or alias) — use it for 'players on <team>' or 'international players "
            "on <team>'. At least one filter is expected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "name substring"},
                "season": {"type": "integer"},
                "position": {"type": "string", "description": "e.g. OH, MB, S, L, DS, OPP"},
                "class_year": {"type": "string"},
                "conference": {"type": "string"},
                "team": {"type": "string", "description": "team name/short name/alias, e.g. 'Nebraska'"},
                "state": {"type": "string", "description": "home state (name or 2-letter code)"},
                "hometown": {"type": "string", "description": "hometown substring, e.g. a city"},
                "country": {"type": "string", "description": "home country, e.g. 'Canada'; 'USA' = domestic"},
                "international": {"type": "boolean", "description": "true = only players from outside the US"},
                "min_height_inches": {"type": "integer", "description": "minimum height (6-6 = 78)"},
                "max_height_inches": {"type": "integer", "description": "maximum height in inches"},
                "sort_by": {"type": "string", "description": "'height' = tallest first; else by name"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "list_teams",
        "description": (
            "List or search team identities (exact name, short name, conference). Use this to ground "
            "a fuzzy name BEFORE another tool: to confirm a school's exact name, or to resolve an "
            "abbreviation/nickname you're unsure maps to a real team (e.g. is 'IU' Indiana or Iona? "
            "search 'query=Indiana'). Pass a substring (matches name/short/alias) and/or a conference; "
            "omit both to list all teams."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "name/short/alias substring, e.g. 'Indiana'"},
                "conference": {"type": "string", "description": "conference name or abbrev, e.g. 'MAC'"},
                "limit": {"type": "integer", "description": "default 50, max 100"},
            },
        },
    },
    {
        "name": "team_records",
        "description": (
            "Team season win/loss records, set records, conference splits, streaks, and rankings "
            "(RPI and AVCA Coaches Poll rank). Use for standings and 'who's ranked' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
            },
        },
    },
    {
        "name": "team_stats",
        "description": (
            "Team-aggregate season stats (totals summed over the roster), ranked by sort_by. Use "
            "for 'which team has the most kills/blocks/aces' or 'best hitting team' (sort_by=hit_pct)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "sort_by": {"type": "string",
                            "description": "kills|assists|aces|digs|total_blocks|pts|hit_pct"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "team_heights",
        "description": (
            "Per-team roster height, ranked: each team's average height and tallest player. Use for "
            "'tallest team' / 'biggest team' (sort_by=avg_height), 'shortest team' (sort_by=avg_height, "
            "take the lowest), or 'team with the tallest player' (sort_by=max_height). Optional "
            "'conference' and 'position' filters (e.g. position='MB' for 'tallest middles'). Only "
            "players with a recorded height are counted (players_measured gives the sample size)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "position": {"type": "string", "description": "e.g. OH, MB, S, L, DS, OPP"},
                "sort_by": {"type": "string", "description": "avg_height (default) | max_height"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "game_highs",
        "description": (
            "Best single-MATCH individual performances, ranked (single games, not season totals). "
            "Use for 'most kills in a single match', 'best single-game dig total', 'top single-match "
            "performances in the MAC'. stat: kills|assists|digs|aces|total_blocks|pts. Optional "
            "conference/team/position filters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stat": {"type": "string",
                         "description": "kills|assists|digs|aces|total_blocks|pts|errors|total_attacks"},
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "team": {"type": "string", "description": "team name/short name/alias"},
                "position": {"type": "string", "description": "e.g. OH, MB, S, L, DS, OPP"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "double_doubles",
        "description": (
            "Players ranked by number of double-doubles (and triple-doubles) this season. A double-"
            "double is a match with >=10 in at least two of kills/assists/digs/aces/total blocks; a "
            "triple-double is three such categories. Use for 'who has the most double-doubles', 'any "
            "triple-doubles this year'. Optional conference/team filters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "team": {"type": "string", "description": "team name/short name/alias"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "team_roster_makeup",
        "description": (
            "Per-team roster demographics, ranked: roster size, international count/percentage, and "
            "average class year (1=Fr..5=Gr). Use for 'which team has the most international "
            "players', 'youngest team', 'oldest/most experienced team', 'biggest roster'. sort_by: "
            "international|international_pct|youngest|oldest|size. Optional conference filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "sort_by": {"type": "string",
                            "description": "international|international_pct|youngest|oldest|size"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "player_origins",
        "description": (
            "Where players come from, grouped and counted. group_by='state' (US home state) or "
            "'country'. Use for 'which state sends the most players' (optionally to a conference), "
            "'how many countries are represented', 'most common home state in the Big Ten'. Optional "
            "conference filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string", "description": "'state' or 'country'"},
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
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
    {
        "name": "player_stats",
        "description": (
            "A single player's full season-to-date totals and per-set rates plus bio (needs "
            "player_id from search_players). Use for 'what are X's stats/totals this season'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "integer"},
                "season": {"type": "integer"},
            },
            "required": ["player_id"],
        },
    },
    {
        "name": "team_schedule",
        "description": (
            "A team's schedule — played results and upcoming games. Use for 'who does <team> play', "
            "'<team>'s next game', 'when does <team> play this week' (filter the returned dates)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "team name or short name, e.g. 'Nebraska'"},
                "season": {"type": "integer"},
                "upcoming_only": {"type": "boolean", "description": "omit played results if true"},
            },
            "required": ["team"],
        },
    },
    {
        "name": "games_on_date",
        "description": (
            "Every D1 game on a date (YYYY-MM-DD): finals + scheduled games. Use for 'what games "
            "are on <date>' or 'who plays <weekday>' (resolve the weekday to a date first)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "season": {"type": "integer"},
            },
            "required": ["date"],
        },
    },
]

_DISPATCH = {
    "leaderboard": leaderboard,
    "search_players": search_players,
    "list_teams": list_teams,
    "team_records": team_records,
    "team_stats": team_stats,
    "team_heights": team_heights,
    "game_highs": game_highs,
    "double_doubles": double_doubles,
    "team_roster_makeup": team_roster_makeup,
    "player_origins": player_origins,
    "player_game_log": player_game_log,
    "player_stats": player_stats,
    "team_schedule": team_schedule,
    "games_on_date": games_on_date,
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
