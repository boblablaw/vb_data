"""Read-only stat query tools, exposed to LLMs as callable tools.

One registry (``TOOL_SPECS``) + one dispatcher (``run_tool``) drives both front-doors:
  * the MCP server (``vb.mcp.server``) turns each spec into an MCP tool, and
  * the in-app Ask box (``vb.api.routers.ask``) hands the specs to Claude as tool definitions.

Every tool is a plain function that takes a SQLAlchemy Session and returns JSON-serializable data;
none of them mutate. Filters (``class_year``, ``position``, ``conference``) make natural-language
questions like *"freshmen with the most kills so far"* answerable.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date as _date
from datetime import timedelta

from sqlalchemy import Text, and_, case, cast, desc, func, not_, nulls_last, or_, select
from sqlalchemy.orm import Session

from ..api.routers.stats import compute_team_records
from ..models import (
    Conference,
    Contest,
    ContestWeek,
    PbpEvent,
    Player,
    PlayerGameStat,
    PlayerSeasonStat,
    RankingSnapshot,
    Schedule,
    Team,
    TeamSeasonId,
)
from ..util import current_season, normalize_class, normalize_school_key

_RANKABLE = {
    "kills", "errors", "total_attacks", "assists", "aces", "serr", "digs", "retatt", "rerr",
    "block_solos", "block_assists", "total_blocks", "berr", "pts", "bhe", "hit_pct",
    "kills_per_set", "assists_per_set", "aces_per_set", "digs_per_set", "blocks_per_set",
    "pts_per_set",
}
# Derived (non-column) leaderboard stats computed on the fly. rec_net = serve-receive "passing"
# quality: receptions (retatt) minus reception errors (rerr).
_COMPUTED_STATS = {"rec_net"}
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
    """Top players for a season by a stat, with optional class/position/conference/hometown filters.

    Volleyball vocabulary for ``stat``: a "passer" / "passing" / "serve receive" is ranked by
    ``rec_net`` (receptions minus reception errors) — NOT assists; the raw parts are ``retatt``
    (receptions) and ``rerr`` (reception errors). A "setter" / "setting" is ``assists``; a
    "defender" / libero is ``digs``; hitting is ``kills`` or ``hit_pct``; serving is ``aces`` (and
    ``serr`` = service errors); blocking is ``total_blocks``.
    """
    if stat not in _RANKABLE and stat not in _COMPUTED_STATS:
        return {"error": f"unknown stat '{stat}'. Valid: {sorted(_RANKABLE | _COMPUTED_STATS)}"}
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    msv = PlayerSeasonStat
    # rec_net = serve-receive "passing" quality: receptions minus reception errors.
    value = (func.coalesce(msv.retatt, 0) - func.coalesce(msv.rerr, 0)) if stat == "rec_net" \
        else getattr(msv, stat)
    stmt = (
        select(
            Player.name, Player.position, Player.class_year, Player.hometown, Player.high_school,
            Team.name.label("team"), Conference.name.label("conference"),
            msv.gp.label("games"), msv.sp.label("sets"),
            msv.retatt.label("receptions"), msv.rerr.label("reception_errors"),
            value.label("value"),
        )
        .select_from(msv)
        .join(Player, Player.id == msv.player_id)
        .join(Team, Team.id == Player.team_id, isouter=True)
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
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
            "receptions": float(r.receptions) if r.receptions is not None else None,
            "reception_errors": float(r.reception_errors) if r.reception_errors is not None else None,
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
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
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


def team_records(
    db: Session, *, season: int | None = None, conference: str | None = None,
    sort_by: str = "wins", limit: int = 25,
) -> list[dict]:
    """Team season records (W-L, sets, conference splits, streak) derived from match linescores.

    Each row includes ``set_pct`` (sets won / sets played) and ``win_pct`` (match win %). ``sort_by``
    ranks the result: 'wins' (default), 'set_pct' (best set win %), or 'win_pct' (best match win %).
    Use for 'best teams', 'best teams by set win %', 'best record in the Big Ten'. Optional
    conference filter."""
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    teams = {
        r.id: {
            "name": r.name, "team_short": r.short_name, "conference": r.conference,
            "conference_id": r.conference_id, "rpi_rank": r.rpi_rank, "rpi_record": r.rpi_record,
            "avca_rank": r.avca_rank,
        }
        for r in db.execute(
            select(
                Team.id, Team.name, Team.short_name, Conference.name.label("conference"),
                func.coalesce(TeamSeasonId.conference_id, Team.conference_id).label("conference_id"),
                Team.rpi_rank, Team.rpi_record, Team.avca_rank,
            )
            .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                     TeamSeasonId.season == season), isouter=True)
            .join(Conference, Conference.id == func.coalesce(
                TeamSeasonId.conference_id, Team.conference_id), isouter=True)
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
    for r in records:
        g = r["wins"] + r["losses"]
        r["win_pct"] = round(r["wins"] / g, 3) if g else None
    sort_by = str(sort_by).lower()
    if sort_by == "set_pct":
        records.sort(key=lambda r: (-(r["set_pct"] or 0), -r["wins"]))
    elif sort_by == "win_pct":
        records.sort(key=lambda r: (-(r["win_pct"] or 0), -r["wins"]))
    else:
        records.sort(key=lambda r: (-r["wins"], r["losses"]))
    # Trim to the fields useful in an NL answer.
    return [
        {k: r[k] for k in (
            "team", "conference", "wins", "losses", "win_pct", "sets_won", "sets_lost",
            "set_pct", "conf_wins", "conf_losses", "win_streak", "rpi_rank", "avca_rank",
        )}
        for r in records[:limit]
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
        # Identity lookup for name grounding — season-agnostic, so use the current/global conference.
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
    team: str | None = None, sort_by: str = "kills", limit: int = 25,
) -> list[dict]:
    """Team-aggregate season stats (summed over the roster's game stats), ranked by ``sort_by``.

    ``sort_by`` is one of kills, assists, aces, digs, total_blocks, pts, hit_pct. Use for questions
    like 'which team has the most kills' or 'best hitting team'. To look up ONE specific team's
    aggregate stats (e.g. 'what is Bowling Green's hitting percentage'), pass ``team`` — this returns
    just that team regardless of where it ranks, so never conclude a named team is missing from a
    top-N leaderboard; query it by ``team`` instead.

    ``total_blocks`` is the official team block figure: solo blocks + block assists / 2 (a block
    assist is credited to every player on the block, so it is half-weighted at the team level)."""
    if sort_by not in _TEAM_AGG:
        return {"error": f"unknown sort_by '{sort_by}'. Valid: {sorted(_TEAM_AGG)}"}
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    team_id = None
    if team:
        team_id = _resolve_team_id(db, team)
        if team_id is None:
            return {"error": f"no team matched '{team}'"}
    pgs = PlayerGameStat
    kills, errors, ta = func.sum(pgs.kills), func.sum(pgs.errors), func.sum(pgs.total_attacks)
    hit_pct = func.nullif(ta, 0)
    stmt = (
        select(
            Team.name.label("team"), Conference.name.label("conference"),
            func.count(func.distinct(pgs.contest_id)).label("games"),
            kills.label("kills"), func.sum(pgs.assists).label("assists"),
            func.sum(pgs.aces).label("aces"), func.sum(pgs.digs).label("digs"),
            # Official team blocks: solo blocks + block assists / 2 (a block assist credits every
            # player on the block, so summing per-player totals would double-count assisted blocks).
            (func.sum(pgs.block_solos) + func.sum(pgs.block_assists) / 2.0).label("total_blocks"),
            func.sum(pgs.pts).label("pts"), errors.label("errors"), ta.label("total_attacks"),
            ((kills - errors) / hit_pct).label("hit_pct"),
        )
        .select_from(pgs)
        .join(Team, Team.id == pgs.team_id)
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
        .where(pgs.season == season)
        .group_by(Team.name, Conference.name)
    )
    if team_id is not None:
        stmt = stmt.where(pgs.team_id == team_id)
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    order = {
        "kills": kills, "assists": func.sum(pgs.assists), "aces": func.sum(pgs.aces),
        "digs": func.sum(pgs.digs),
        "total_blocks": func.sum(pgs.block_solos) + func.sum(pgs.block_assists) / 2.0,
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
    team: str | None = None, position: str | None = None,
    sort_by: str = "avg_height", limit: int = 25,
) -> list[dict]:
    """Per-team roster height, ranked — average height and tallest player on each roster.

    Use for 'tallest team', 'shortest team' (sort_by=avg_height, read from the bottom), 'which team
    is biggest', or 'team with the tallest player' (sort_by=max_height). Optional ``conference``,
    ``team`` and ``position`` filters — e.g. position='MB' answers 'which team has the tallest
    middles'. Only players with a recorded height count; ``players_measured`` shows the sample size
    so a team with very few measured players can be discounted.

    NOTE: this returns only height NUMBERS, not any player's name. To identify WHO the tallest
    player is (e.g. 'who is the tallest player in the MAC', 'name Kent State's tallest'), use
    search_players with sort_by='height' and a conference/team filter instead — it returns names."""
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    team_id = None
    if team:
        team_id = _resolve_team_id(db, team)
        if team_id is None:
            return {"error": f"no team matched '{team}'"}
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
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
        .where(Player.season == season, Player.height_inches.is_not(None))
        .group_by(Team.name, Conference.name)
    )
    if team_id is not None:
        stmt = stmt.where(Player.team_id == team_id)
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
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
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
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
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
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
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
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
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
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
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


_TRANSFER_SORTS = {
    "pts_per_set", "kills_per_set", "assists_per_set", "digs_per_set", "aces_per_set",
    "blocks_per_set", "hit_pct", "pts", "kills", "assists", "digs", "aces", "total_blocks",
}


def _prior_transfer_index(db: Session, prior_season: int) -> dict:
    """In-memory index of a prior season's players keyed for strict identity lookup.

    Returns two dicts: by (name, hometown, high_school) and by (name, hometown), each mapping to the
    list of prior-season players sharing that key (with team_id/name preloaded). Used to resolve, for
    every current-season player, whether they appeared last season at a different school — the same
    conservative match as ``routers.players._match_identity_strict`` (requires a hometown; a key that
    maps to more than one prior player is ambiguous and ignored), but done as one bulk pass."""
    by_hs: dict[tuple, list] = defaultdict(list)
    by_town: dict[tuple, list] = defaultdict(list)
    rows = db.execute(
        select(Player.name, Player.hometown, Player.high_school, Player.team_id,
               Team.name.label("team_name"), Team.short_name.label("team_short"))
        .join(Team, Team.id == Player.team_id, isouter=True)
        .where(Player.season == prior_season, Player.hometown.isnot(None))
    ).all()
    for r in rows:
        town_key = (r.name, r.hometown)
        by_town[town_key].append(r)
        if r.high_school:
            by_hs[(r.name, r.hometown, r.high_school)].append(r)
    return {"by_hs": by_hs, "by_town": by_town}


def _prior_school(idx: dict, name: str, hometown: str | None, high_school: str | None):
    """The single prior-season player matching this identity, or None (mirrors _match_identity_strict).

    hometown+high_school first (most specific), then hometown; a key with more than one match is
    ambiguous → None, so we never guess where a same-named person came from."""
    if not name or not hometown:
        return None
    if high_school:
        hit = idx["by_hs"].get((name, hometown, high_school))
        if hit and len(hit) == 1:
            return hit[0]
    hit = idx["by_town"].get((name, hometown))
    if hit and len(hit) == 1:
        return hit[0]
    return None


def transfer_impact(
    db: Session, *, season: int | None = None, conference: str | None = None,
    position: str | None = None, sort_by: str = "pts_per_set", min_sets: int = 10,
    limit: int = 25,
) -> list[dict]:
    """Transfer players ranked by their impact at their NEW team this season.

    A transfer is someone who appeared LAST season at a DIFFERENT school (matched by durable identity
    — name + hometown, best-effort high school — since player ids don't bridge seasons). Each row
    carries the new team, the ``previous_team`` transferred from, and season stats so "biggest impact"
    can mean whatever fits: pts_per_set (default, best all-around proxy), kills/assists/digs/aces/
    blocks per set, hit_pct, or the season totals. Use for 'which transfer is having the biggest
    impact', 'best transfer in the Big Ten', 'top transfer setters/hitters'. sort_by: one of
    pts_per_set|kills_per_set|assists_per_set|digs_per_set|aces_per_set|blocks_per_set|hit_pct|pts|
    kills|assists|digs|aces|total_blocks. ``min_sets`` drops players who've barely played (rate stats
    are noisy on tiny samples). Optional conference/position filters. Transfers are detected against
    the immediately preceding season."""
    if sort_by not in _TRANSFER_SORTS:
        return {"error": f"unknown sort_by '{sort_by}'. Valid: {sorted(_TRANSFER_SORTS)}"}
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    min_sets = max(0, int(min_sets))
    idx = _prior_transfer_index(db, season - 1)
    if not idx["by_town"]:
        return []  # no prior-season data to compare against

    ss = PlayerSeasonStat
    stmt = (
        select(
            Player.id, Player.name, Player.hometown, Player.high_school,
            Player.position, Player.class_year, Player.team_id,
            Team.name.label("team"), Team.short_name.label("team_short"),
            ss.sp, ss.gp, ss.kills, ss.errors, ss.total_attacks, ss.hit_pct, ss.assists,
            ss.aces, ss.digs, ss.total_blocks, ss.pts, ss.kills_per_set, ss.assists_per_set,
            ss.aces_per_set, ss.digs_per_set, ss.blocks_per_set, ss.pts_per_set,
        )
        .select_from(Player)
        .join(Team, Team.id == Player.team_id, isouter=True)
        .join(ss, and_(ss.player_id == Player.id, ss.season == season), isouter=True)
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
        .where(Player.season == season, Player.hometown.isnot(None))
    )
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    if position:
        clause = _position_clause(position)
        if clause is not None:
            stmt = stmt.where(clause)

    out: list[dict] = []
    for r in db.execute(stmt).all():
        prev = _prior_school(idx, r.name, r.hometown, r.high_school)
        if prev is None or prev.team_id == r.team_id:
            continue  # not a transfer (no prior match, or returning to the same school)
        if min_sets and (r.sp or 0) < min_sets:
            continue
        out.append({
            "player_id": r.id, "player": r.name, "position": r.position,
            "class_year": r.class_year, "team": r.team, "team_short": r.team_short,
            "previous_team": prev.team_name, "previous_team_short": prev.team_short,
            "sp": float(r.sp) if r.sp is not None else None,
            "gp": int(r.gp) if r.gp is not None else None,
            "kills": float(r.kills) if r.kills is not None else None,
            "assists": float(r.assists) if r.assists is not None else None,
            "digs": float(r.digs) if r.digs is not None else None,
            "aces": float(r.aces) if r.aces is not None else None,
            "total_blocks": float(r.total_blocks) if r.total_blocks is not None else None,
            "pts": float(r.pts) if r.pts is not None else None,
            "hit_pct": round(float(r.hit_pct), 3) if r.hit_pct is not None else None,
            "kills_per_set": round(float(r.kills_per_set), 2) if r.kills_per_set is not None else None,
            "assists_per_set": round(float(r.assists_per_set), 2) if r.assists_per_set is not None else None,
            "aces_per_set": round(float(r.aces_per_set), 2) if r.aces_per_set is not None else None,
            "digs_per_set": round(float(r.digs_per_set), 2) if r.digs_per_set is not None else None,
            "blocks_per_set": round(float(r.blocks_per_set), 2) if r.blocks_per_set is not None else None,
            "pts_per_set": round(float(r.pts_per_set), 2) if r.pts_per_set is not None else None,
        })
    out.sort(key=lambda d: (d.get(sort_by) is None, -(d.get(sort_by) or 0)))
    out = out[:limit]
    for i, d in enumerate(out):
        d["rank"] = i + 1
    return out


_DEFENSE_SORTS = {"opp_hit_pct", "opp_kills", "opp_total_attacks"}


def team_defense(
    db: Session, *, season: int | None = None, conference: str | None = None,
    sort_by: str = "opp_hit_pct", min_games: int = 1, limit: int = 25,
) -> list[dict]:
    """Team defense: opponents' aggregate offense against each team, ranked best-defense-first.

    For every team, sums the OTHER side's kills/errors/attacks from each match's box score, so
    ``opp_hit_pct`` = the hitting percentage a team holds its opponents to. Lower is better, so
    results are ordered ascending by ``sort_by`` (best defense first). Use for 'best opponent
    hitting percentage', 'which team forces opponents into the worst hitting', 'best blocking/
    defensive team by opponent efficiency'. sort_by: opp_hit_pct|opp_kills|opp_total_attacks.
    Optional conference filter; ``min_games`` drops teams with too few matches."""
    if sort_by not in _DEFENSE_SORTS:
        return {"error": f"unknown sort_by '{sort_by}'. Valid: {sorted(_DEFENSE_SORTS)}"}
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    pgs = PlayerGameStat
    # Per (contest, team) box-score totals, then self-join each team to its opponent's line.
    per = (
        select(
            pgs.contest_id.label("cid"), pgs.team_id.label("tid"),
            func.sum(pgs.kills).label("k"), func.sum(pgs.errors).label("e"),
            func.sum(pgs.total_attacks).label("ta"),
        )
        .where(pgs.season == season)
        .group_by(pgs.contest_id, pgs.team_id)
        .subquery()
    )
    me, opp = per.alias("me"), per.alias("opp")
    oppk, oppe, oppta = func.sum(opp.c.k), func.sum(opp.c.e), func.sum(opp.c.ta)
    opp_hit = (oppk - oppe) / func.nullif(oppta, 0)
    games = func.count(func.distinct(me.c.cid))
    order_expr = {
        "opp_hit_pct": opp_hit, "opp_kills": oppk, "opp_total_attacks": oppta,
    }[sort_by]
    stmt = (
        select(
            Team.name.label("team"), Conference.name.label("conference"),
            games.label("games"), oppk.label("opp_kills"), oppe.label("opp_errors"),
            oppta.label("opp_total_attacks"), opp_hit.label("opp_hit_pct"),
        )
        .select_from(me)
        .join(opp, and_(opp.c.cid == me.c.cid, opp.c.tid != me.c.tid))
        .join(Team, Team.id == me.c.tid)
        .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                 TeamSeasonId.season == season), isouter=True)
        .join(Conference, Conference.id == func.coalesce(
            TeamSeasonId.conference_id, Team.conference_id), isouter=True)
        .group_by(Team.name, Conference.name)
        .having(games >= max(1, int(min_games)))
    )
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            stmt = stmt.where(clause)
    # Lower opponent output = better defense, so ascending is "best first".
    stmt = stmt.order_by(nulls_last(order_expr)).limit(limit)
    return [
        {
            "rank": i + 1, "team": r.team, "conference": r.conference,
            "games": int(r.games) if r.games is not None else None,
            "opp_kills": float(r.opp_kills) if r.opp_kills is not None else None,
            "opp_errors": float(r.opp_errors) if r.opp_errors is not None else None,
            "opp_total_attacks": float(r.opp_total_attacks) if r.opp_total_attacks is not None else None,
            "opp_hit_pct": round(float(r.opp_hit_pct), 3) if r.opp_hit_pct is not None else None,
        }
        for i, r in enumerate(db.execute(stmt).all())
    ]


def _rank_as_of(snaps: list[tuple], day) -> tuple:
    """Most recent (rpi_rank, avca_rank) with as_of <= day, from an ascending-sorted list."""
    if not snaps or day is None:
        return (None, None)
    best = None
    for as_of, rpi, avca in snaps:
        if as_of <= day:
            best = (rpi, avca)
        else:
            break
    return best or (None, None)


def compute_quality_wins(
    db: Session, *, team: str | None = None, conference: str | None = None,
    poll: str = "avca", threshold: int = 25, season: int | None = None,
) -> list[dict]:
    """Per-team quality wins: wins over an opponent that was ranked *as of the game date*.

    Rank-at-the-time comes from ``ranking_snapshots`` (history only exists from the first
    snapshot; earlier games can't count). ``poll`` selects avca_rank or rpi_rank; a win counts
    when the beaten team's rank that day is not null and <= ``threshold``. Returns one entry per
    winning team, sorted by quality-win count desc. Shared by the quality_wins tool and the
    /teams/{id}/quality-wins endpoint."""
    poll = "rpi" if str(poll).lower() == "rpi" else "avca"
    threshold = max(1, int(threshold))
    season = _season(season)

    snaps: dict[int, list] = {}
    for s in db.execute(
        select(RankingSnapshot.team_id, RankingSnapshot.as_of,
               RankingSnapshot.rpi_rank, RankingSnapshot.avca_rank)
        .where(RankingSnapshot.season == season)
    ).all():
        snaps.setdefault(s.team_id, []).append((s.as_of, s.rpi_rank, s.avca_rank))
    for v in snaps.values():
        v.sort(key=lambda x: x[0])

    # Optional filters on the WINNING team.
    only_team_id = _resolve_team_id(db, team) if team else None
    if team and only_team_id is None:
        return {"error": f"no team matched '{team}'"}
    conf_team_ids = None
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            conf_team_ids = {
                tid for (tid,) in db.execute(
                    select(Team.id)
                    .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                             TeamSeasonId.season == season), isouter=True)
                    .join(Conference, Conference.id == func.coalesce(
                        TeamSeasonId.conference_id, Team.conference_id))
                    .where(clause)
                ).all()
            }

    rows = db.execute(
        select(
            Contest.contest_id, ContestWeek.game_date,
            Contest.home_team_id, Contest.away_team_id,
            Contest.home_sets_won, Contest.away_sets_won,
        )
        .select_from(Contest)
        .join(ContestWeek, ContestWeek.contest_id == Contest.contest_id, isouter=True)
        .where(
            Contest.season == season,
            Contest.home_sets_won.is_not(None), Contest.away_sets_won.is_not(None),
            Contest.home_team_id.is_not(None), Contest.away_team_id.is_not(None),
        )
    ).all()

    agg: dict[int, list] = {}
    for r in rows:
        hsw, asw = r.home_sets_won, r.away_sets_won
        if hsw == asw:
            continue
        if hsw > asw:
            winner, loser, wscore, lscore = r.home_team_id, r.away_team_id, hsw, asw
        else:
            winner, loser, wscore, lscore = r.away_team_id, r.home_team_id, asw, hsw
        if only_team_id is not None and winner != only_team_id:
            continue
        if conf_team_ids is not None and winner not in conf_team_ids:
            continue
        rpi, avca = _rank_as_of(snaps.get(loser), r.game_date)
        rank_at_time = avca if poll == "avca" else rpi
        if rank_at_time is None or rank_at_time > threshold:
            continue
        agg.setdefault(winner, []).append({
            "opponent_id": loser, "rank_at_time": int(rank_at_time), "poll": poll,
            "date": r.game_date.isoformat() if r.game_date else None,
            "score": f"{wscore}-{lscore}", "contest_id": r.contest_id,
        })

    # Resolve team meta for winners + opponents in one pass.
    need = set(agg) | {w["opponent_id"] for wins in agg.values() for w in wins}
    meta = {
        t.id: t for t in db.scalars(
            select(Team).where(Team.id.in_(need or {-1}))
        ).all()
    }
    conf_of = {
        c.id: c.name for c in db.scalars(select(Conference)).all()
    }

    out = []
    for tid, wins in agg.items():
        t = meta.get(tid)
        wins.sort(key=lambda w: (w["rank_at_time"], w["date"] or ""))
        for w in wins:
            o = meta.get(w["opponent_id"])
            w["opponent"] = o.name if o else None
            w["opponent_short"] = o.short_name if o else None
            w["opponent_logo_light"] = o.logo_light if o else None
            w["opponent_logo_dark"] = o.logo_dark if o else None
        out.append({
            "team_id": tid,
            "team": t.name if t else None,
            "team_short": t.short_name if t else None,
            "conference": conf_of.get(t.conference_id) if t else None,
            "quality_wins": len(wins),
            "wins": wins,
        })
    out.sort(key=lambda e: (-e["quality_wins"], e["team"] or ""))
    return out


def quality_wins(
    db: Session, *, team: str | None = None, conference: str | None = None,
    poll: str = "avca", threshold: int = 25, season: int | None = None, limit: int = 25,
) -> list[dict]:
    """Teams ranked by quality wins — wins over a team that was ranked *at the time of the game*.

    A quality win = beating an opponent whose ranking on that game's date was in the top
    ``threshold`` of the chosen ``poll`` (avca = AVCA Coaches Poll top 25; rpi = NCAA RPI, which
    ranks every team, so use a larger threshold like 25/50). Rank-at-the-time only exists from when
    snapshots began, so very early-season games may not count. Use for 'who has the best quality
    wins', 'best wins in the Big Ten', 'which team has beaten the most ranked teams'. Optional
    team/conference filters."""
    limit = max(1, min(int(limit), _MAX_LIMIT))
    res = compute_quality_wins(
        db, team=team, conference=conference, poll=poll, threshold=threshold, season=season,
    )
    if isinstance(res, dict):  # error passthrough
        return res
    return res[:limit]


def biggest_upsets(
    db: Session, *, poll: str = "avca", threshold: int = 25, min_gap: int = 1,
    team: str | None = None, conference: str | None = None,
    season: int | None = None, limit: int = 10,
) -> list[dict]:
    """Biggest UPSETS — games where the winner was ranked worse than the loser AT THE TIME.

    Rank-at-the-time comes from ranking history (``ranking_snapshots``). ``poll='avca'`` (default)
    is the meaningful one: wins over an AVCA Coaches Poll top-``threshold`` team (the winner may be
    unranked), ordered by the beaten team's rank. ``poll='rpi'`` uses NCAA RPI, which ranks every
    team and yields a numeric ``gap`` (winner's rank minus loser's rank) for essentially every game
    — but note early-season RPI is last year's rollover, so its gaps are unreliable until real RPI
    stabilizes later in the season. Use for 'biggest upsets so far', 'craziest upset this season',
    'biggest upset in the Big Ten', 'has <team> pulled off any upsets'. Optional team (winner) /
    conference (winner) filters. History only starts from the first snapshot, so very early-season
    games may not have a rank-at-the-time."""
    poll = "rpi" if str(poll).lower() == "rpi" else "avca"
    threshold = max(1, int(threshold))
    min_gap = max(1, int(min_gap))
    season = _season(season)
    limit = max(1, min(int(limit), _MAX_LIMIT))

    snaps: dict[int, list] = {}
    for s in db.execute(
        select(RankingSnapshot.team_id, RankingSnapshot.as_of,
               RankingSnapshot.rpi_rank, RankingSnapshot.avca_rank)
        .where(RankingSnapshot.season == season)
    ).all():
        snaps.setdefault(s.team_id, []).append((s.as_of, s.rpi_rank, s.avca_rank))
    for v in snaps.values():
        v.sort(key=lambda x: x[0])

    only_team_id = _resolve_team_id(db, team) if team else None
    if team and only_team_id is None:
        return {"error": f"no team matched '{team}'"}
    conf_team_ids = None
    if conference:
        clause = _conference_clause(conference)
        if clause is not None:
            conf_team_ids = {
                tid for (tid,) in db.execute(
                    select(Team.id)
                    .join(TeamSeasonId, and_(TeamSeasonId.team_id == Team.id,
                                             TeamSeasonId.season == season), isouter=True)
                    .join(Conference, Conference.id == func.coalesce(
                        TeamSeasonId.conference_id, Team.conference_id))
                    .where(clause)
                ).all()
            }

    rows = db.execute(
        select(
            Contest.contest_id, ContestWeek.game_date,
            Contest.home_team_id, Contest.away_team_id,
            Contest.home_sets_won, Contest.away_sets_won,
        )
        .select_from(Contest)
        .join(ContestWeek, ContestWeek.contest_id == Contest.contest_id, isouter=True)
        .where(
            Contest.season == season,
            Contest.home_sets_won.is_not(None), Contest.away_sets_won.is_not(None),
            Contest.home_team_id.is_not(None), Contest.away_team_id.is_not(None),
        )
    ).all()

    upsets: list[dict] = []
    for r in rows:
        hsw, asw = r.home_sets_won, r.away_sets_won
        if hsw == asw:
            continue
        if hsw > asw:
            winner, loser, wscore, lscore = r.home_team_id, r.away_team_id, hsw, asw
        else:
            winner, loser, wscore, lscore = r.away_team_id, r.home_team_id, asw, hsw
        if only_team_id is not None and winner != only_team_id:
            continue
        if conf_team_ids is not None and winner not in conf_team_ids:
            continue

        w_rpi, w_avca = _rank_as_of(snaps.get(winner), r.game_date)
        l_rpi, l_avca = _rank_as_of(snaps.get(loser), r.game_date)
        gap = (w_rpi - l_rpi) if (w_rpi is not None and l_rpi is not None) else None

        if poll == "avca":
            # Upset over a ranked (AVCA top-N) team; winner may be unranked or lower.
            if l_avca is None or l_avca > threshold:
                continue
            if w_avca is not None and w_avca <= l_avca:
                continue
        else:
            if gap is None or gap < min_gap:
                continue

        upsets.append({
            "winner_id": winner, "loser_id": loser,
            "winner_rpi": w_rpi, "winner_avca": w_avca,
            "loser_rpi": l_rpi, "loser_avca": l_avca,
            "gap": gap, "poll": poll,
            "date": r.game_date.isoformat() if r.game_date else None,
            "score": f"{wscore}-{lscore}", "contest_id": r.contest_id,
        })

    # Order by RPI magnitude when we have it; AVCA-only entries fall back to the loser's poll rank.
    def _key(u: dict) -> tuple:
        if u["gap"] is not None:
            return (0, -u["gap"], u["loser_avca"] or 999, u["date"] or "")
        return (1, u["loser_avca"] or 999, u["date"] or "")
    upsets.sort(key=_key)
    upsets = upsets[:limit]

    need = {u["winner_id"] for u in upsets} | {u["loser_id"] for u in upsets}
    meta = {t.id: t for t in db.scalars(select(Team).where(Team.id.in_(need or {-1}))).all()}
    for u in upsets:
        w, lo = meta.get(u["winner_id"]), meta.get(u["loser_id"])
        u["winner"] = w.name if w else None
        u["winner_short"] = w.short_name if w else None
        u["winner_logo_light"] = w.logo_light if w else None
        u["winner_logo_dark"] = w.logo_dark if w else None
        u["loser"] = lo.name if lo else None
        u["loser_short"] = lo.short_name if lo else None
        u["loser_logo_light"] = lo.logo_light if lo else None
        u["loser_logo_dark"] = lo.logo_dark if lo else None
    return upsets


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


def match_pbp(
    db: Session, *, team: str, date: str, opponent: str | None = None,
    season: int | None = None, include_rally_log: bool = False,
) -> dict:
    """Play-by-play breakdown of ONE match, resolved by team + date (YYYY-MM-DD).

    Returns per-set momentum stats (points, ties, lead changes, each team's biggest scoring run,
    and the point-type breakdown: kills/aces/blocks/opponent errors) plus the match scoring leaders
    (who actually put the points away). Use for 'how many lead changes', 'biggest run', 'who scored
    the points', 'was it close', 'momentum' questions about a specific game. Pass ``opponent`` to
    disambiguate a doubleheader. ``include_rally_log=true`` adds a capped point-by-point log; leave
    it off unless the user wants the sequence, since it is long."""
    season = _season(season)
    tid = _resolve_team_id(db, team)
    if tid is None:
        return {"error": f"no team matching '{team}'"}
    opp_id = None
    if opponent:
        opp_id = _resolve_team_id(db, opponent)
        if opp_id is None:
            return {"error": f"no team matching '{opponent}'"}

    names = {t.id: t.name for t in db.scalars(select(Team)).all()}
    contests = db.scalars(
        select(Contest).where(
            Contest.season == season,
            Contest.date.like(f"{date}%"),
            or_(Contest.home_team_id == tid, Contest.away_team_id == tid),
        )
    ).all()
    if opp_id is not None:
        contests = [c for c in contests if opp_id in (c.home_team_id, c.away_team_id)]
    if not contests:
        return {"error": f"no {season} match found for '{team}' on {date}"}
    if len(contests) > 1:
        return {"matches": [
            {"contest_id": c.contest_id, "date": c.date,
             "away": names.get(c.away_team_id), "home": names.get(c.home_team_id),
             "hint": "multiple games this day — pass 'opponent' to pick one"}
            for c in contests
        ]}

    c = contests[0]
    meta = {
        "contest_id": c.contest_id, "date": c.date,
        "away_team": names.get(c.away_team_id), "home_team": names.get(c.home_team_id),
        "away_sets_won": c.away_sets_won, "home_sets_won": c.home_sets_won,
    }
    events = db.scalars(
        select(PbpEvent).where(PbpEvent.contest_id == c.contest_id)
        .order_by(PbpEvent.set_number, PbpEvent.seq)
    ).all()
    if not events:
        return {**meta, "note": "no play-by-play recorded for this match"}

    sides = {c.away_team_id: "away", c.home_team_id: "home"}

    # Assisting setter per kill rally: last same-team ``set`` touch earlier in the rally (mirrors
    # contests.contest_pbp). Keyed by (set_number, rally_number).
    rally_events: dict[tuple[int, int], list] = defaultdict(list)
    for e in events:
        rally_events[(e.set_number, e.rally_number)].append(e)
    assist_for: dict[tuple[int, int], object] = {}
    for key, revs in rally_events.items():
        term = next((e for e in revs if e.is_terminal), None)
        if term is None or term.terminal_type != "kill" or term.scoring_team_id is None:
            continue
        setter = next(
            (e for e in reversed(revs)
             if e.touch_type == "set" and e.team_id == term.scoring_team_id),
            None,
        )
        if setter is not None:
            assist_for[key] = setter

    # Match scoring leaders: kill/ace/block terminals credited to the player who scored.
    leaders: dict[tuple, dict] = {}

    def _pt_bucket():
        return {"kills": 0, "aces": 0, "blocks": 0, "opp_errors": 0}

    sets_out: list[dict] = []
    rally_log: list[str] = []
    for set_no in sorted({e.set_number for e in events}):
        set_events = [e for e in events if e.set_number == set_no]
        points = {"away": 0, "home": 0}
        point_types = {"away": _pt_bucket(), "home": _pt_bucket()}
        run = {"away": 0, "home": 0}
        cur_side, cur_len = None, 0
        ties = lead_changes = 0
        prev_leader = 0  # 0 tie, 1 away ahead, -1 home ahead
        for e in set_events:
            if not e.is_terminal:
                continue
            scorer_side = sides.get(e.scoring_team_id)
            if scorer_side is None:
                continue
            points[scorer_side] += 1
            # biggest consecutive run per team
            if e.scoring_team_id == cur_side:
                cur_len += 1
            else:
                cur_side, cur_len = e.scoring_team_id, 1
            run[scorer_side] = max(run[scorer_side], cur_len)
            # point-type breakdown from the scoring team's perspective
            tt = e.terminal_type
            if tt == "kill":
                point_types[scorer_side]["kills"] += 1
            elif tt == "ace":
                point_types[scorer_side]["aces"] += 1
            elif tt == "block":
                point_types[scorer_side]["blocks"] += 1
            elif tt and tt.endswith("_error"):
                point_types[scorer_side]["opp_errors"] += 1
            # scoring leaders (only players who actively scored the point)
            if tt in ("kill", "ace", "block") and e.player_name:
                lk = (e.player_name, e.player_id)
                led = leaders.setdefault(lk, {
                    "player": e.player_name, "player_id": e.player_id,
                    "team": names.get(e.scoring_team_id), "points": 0,
                    "kills": 0, "aces": 0, "blocks": 0,
                })
                led["points"] += 1
                led[tt + "s"] += 1
            # ties / lead changes off the running score
            if e.away_score is not None and e.home_score is not None:
                if e.away_score == e.home_score:
                    ties += 1
                    leader = 0
                else:
                    leader = 1 if e.away_score > e.home_score else -1
                if leader != 0 and prev_leader != 0 and leader != prev_leader:
                    lead_changes += 1
                if leader != 0:
                    prev_leader = leader
            if include_rally_log:
                setter = assist_for.get((e.set_number, e.rally_number))
                assist = f" (assist {setter.player_name})" if setter is not None else ""
                verb = {"kill": "Kill", "ace": "Ace", "block": "Block"}.get(
                    tt, (tt or "point").replace("_", " ").title())
                who = e.player_name or names.get(e.scoring_team_id) or "?"
                rally_log.append(
                    f"S{set_no} {e.away_score}-{e.home_score} — {verb} by {who}{assist}"
                )
        sets_out.append({
            "set_number": set_no,
            "away_points": points["away"], "home_points": points["home"],
            "winner": "away" if points["away"] > points["home"] else "home",
            "ties": ties, "lead_changes": lead_changes,
            "biggest_run": {"away": run["away"], "home": run["home"]},
            "point_types": point_types,
        })

    top_leaders = sorted(leaders.values(), key=lambda d: -d["points"])[:6]
    out = {**meta, "sets": sets_out, "scoring_leaders": top_leaders}
    if include_rally_log:
        out["rally_log"] = rally_log
    return out


# Touch-level (non-substitution) event types: presence of any of these means the player was on court.
_LINEUP_TOUCH_TYPES = {"serve", "reception", "set", "attack", "dig", "block", "terminal"}
# Positions that play back-row only (libero / defensive specialist). A pre-serve sub_in by one of
# these covers a starter (who stays in the six); a sub_in by any other position is a real swap.
_LIBERO_POS = {"L", "DS"}


def per_set_lineups(events, away_team_id, home_team_id, roster, team_names) -> dict:
    """Per-set starters/subs and lineup changes for both teams, reconstructed from the pbp sub log.

    A player is a STARTER of a set if they were on court at the first serve, and a SUB if they came
    off the bench mid-set. Classified from each player's FIRST event in the set (lowest ``seq``):

    * a real touch, OR a rally-0 ``sub_out`` (a rotation starter covered pre-serve — the middle the
      libero replaces, or a starter a back-row sub comes in for), OR a rally-0 ``sub_in`` **by a
      libero/DS** (the defensive specialist taking a back-row slot — usually the 7th starter)
      -> **starter**;
    * a rally-0 ``sub_in`` by any other position (a bench player covering a rotation starter in the
      back row — that starter is counted via their own rally-0 ``sub_out``), OR any rally>=1
      ``sub_in`` -> **bench sub**;
    * a rally>=1 ``sub_out`` as the first event (no earlier touch) -> **ignored** (end-of-set serving
      churn / a data gap where the entering ``sub_in`` wasn't logged).

    This correctly keeps a starter who is subbed OUT and back IN within a set (e.g. a setter swap),
    which the older "touched but never subbed in" rule dropped — leaving the setter slot empty. It
    also avoids double-counting a defensive substitution: the rotation starter counts, the bench
    player who covers them does not (so a second back-row sub doesn't push the count past 7). Skips
    null ids and dual-credit "A, B" block rows (those players appear via their own touches).

    ``events`` is any iterable of ``PbpEvent`` (order doesn't matter — first-event is taken by seq).
    ``roster`` is ``{player_id: Player}``; ``team_names`` is ``{team_id: name}``. Returns
    ``{team_name: {"team_id","side","sets":[{set_number,starters,subs}],"starters_changed",
    "starter_changes"}}`` — each player entry carries ``player_id``/``player``/``position``/``number``.
    """
    ev_name: dict[int, str] = {}
    first: dict[tuple[int, int, int], tuple[int, str, int]] = {}  # (set,team,pid) -> (seq,type,rally)
    for e in events:
        if e.player_id is None:
            continue
        if e.player_name and ", " in e.player_name:
            continue
        ev_name.setdefault(e.player_id, e.player_name)
        key = (e.set_number, e.team_id, e.player_id)
        cur = first.get(key)
        if cur is None or e.seq < cur[0]:
            first[key] = (e.seq, e.touch_type, e.rally_number)

    def _pname(pid: int) -> str | None:
        p = roster.get(pid)
        return p.name if p else ev_name.get(pid)

    def _entry(pid: int) -> dict:
        p = roster.get(pid)
        return {"player_id": pid, "player": _pname(pid),
                "position": p.position if p else None, "number": p.number if p else None}

    def _is_libero(pid: int) -> bool:
        # A back-row-only player (libero / defensive specialist). Position may be a combined label
        # like "L/DS", so match on the "/"-split tokens.
        p = roster.get(pid)
        pos = p.position if p else None
        return bool(pos and (set(pos.split("/")) & _LIBERO_POS))

    set_numbers = sorted({sn for (sn, _t, _p) in first})
    sides = {away_team_id: "away", home_team_id: "home"}
    teams_out: dict[str, dict] = {}
    for team_id, side in sides.items():
        starters_by_set: dict[int, set] = {}
        sets_list = []
        for sn in set_numbers:
            starter_ids: set = set()
            sub_ids: set = set()
            for (s, t, pid), (_seq, tt, rally) in first.items():
                if s != sn or t != team_id:
                    continue
                if tt == "sub_in":
                    # A pre-serve (rally 0) sub_in by a libero/DS is the (usually 7th) starter taking
                    # a back-row slot. Any OTHER position entering pre-serve is a regular substitution
                    # covering a rotation starter (that starter is caught by their own rally-0 sub_out
                    # below), so it's a bench SUB — not a starter. A rally>=1 sub_in is also a sub.
                    if rally == 0 and _is_libero(pid):
                        starter_ids.add(pid)
                    else:
                        sub_ids.add(pid)
                elif tt == "sub_out":
                    if rally == 0:
                        starter_ids.add(pid)  # a rotation starter covered pre-serve; still a starter
                    # a rally>=1 sub_out as the first event is end-of-set churn — ignore
                else:
                    starter_ids.add(pid)  # a real touch: on court
            starters = sorted((_entry(pid) for pid in starter_ids),
                              key=lambda d: d["player"] or "")
            subs = sorted((_entry(pid) for pid in sub_ids), key=lambda d: d["player"] or "")
            starters_by_set[sn] = starter_ids
            sets_list.append({"set_number": sn, "starters": starters, "subs": subs})

        base_set = set_numbers[0] if set_numbers else None
        base = starters_by_set.get(base_set, set())
        changes, changed = [], False
        for sn in set_numbers:
            if sn == base_set:
                continue
            cur_ids = starters_by_set.get(sn, set())
            added = sorted(_pname(pid) for pid in (cur_ids - base))
            removed = sorted(_pname(pid) for pid in (base - cur_ids))
            if added or removed:
                changed = True
                changes.append({"set_number": sn, "vs_set": base_set,
                                "added": added, "removed": removed})
        teams_out[team_names.get(team_id)] = {
            "team_id": team_id, "side": side, "sets": sets_list,
            "starters_changed": changed, "starter_changes": changes,
        }
    return teams_out


def match_lineups(
    db: Session, *, team: str, date: str, opponent: str | None = None,
    season: int | None = None,
) -> dict:
    """Per-set lineups (and lineup CHANGES) for ONE match, resolved by team + date (YYYY-MM-DD).

    Reads the play-by-play substitution log, which stats.ncaa.org DOES record. For each team and set
    it returns the STARTERS (players on court at the set's first serve — usually 7, the six plus the
    libero who enters pre-serve for a middle) and the bench players who SUBBED IN mid-set, plus a
    set-by-set diff of the starting group and a ``starters_changed`` flag. A starter is kept even if
    they are subbed out and back in during the set (e.g. a two-setter swap). Use for 'did X change
    their lineup in set N', 'who started each set', 'who came off the bench', 'different starters'. A
    changed STARTING group is the meaningful lineup-change signal — routine rotational subs are not;
    libero/defensive-sub slots are approximate where the feed omits a substitution. Pass ``opponent``
    to disambiguate a doubleheader."""
    season = _season(season)
    tid = _resolve_team_id(db, team)
    if tid is None:
        return {"error": f"no team matching '{team}'"}
    opp_id = None
    if opponent:
        opp_id = _resolve_team_id(db, opponent)
        if opp_id is None:
            return {"error": f"no team matching '{opponent}'"}

    names = {t.id: t.name for t in db.scalars(select(Team)).all()}
    contests = db.scalars(
        select(Contest).where(
            Contest.season == season,
            Contest.date.like(f"{date}%"),
            or_(Contest.home_team_id == tid, Contest.away_team_id == tid),
        )
    ).all()
    if opp_id is not None:
        contests = [c for c in contests if opp_id in (c.home_team_id, c.away_team_id)]
    if not contests:
        return {"error": f"no {season} match found for '{team}' on {date}"}
    if len(contests) > 1:
        return {"matches": [
            {"contest_id": c.contest_id, "date": c.date,
             "away": names.get(c.away_team_id), "home": names.get(c.home_team_id),
             "hint": "multiple games this day — pass 'opponent' to pick one"}
            for c in contests
        ]}

    c = contests[0]
    meta = {
        "contest_id": c.contest_id, "date": c.date,
        "away_team": names.get(c.away_team_id), "home_team": names.get(c.home_team_id),
        "away_sets_won": c.away_sets_won, "home_sets_won": c.home_sets_won,
    }
    events = db.scalars(
        select(PbpEvent).where(PbpEvent.contest_id == c.contest_id)
        .order_by(PbpEvent.set_number, PbpEvent.seq)
    ).all()
    if not events:
        return {**meta, "note": "no play-by-play recorded for this match"}

    # Canonical name/position per player id (roster for both teams this season); per_set_lineups
    # falls back to the name carried on the event if a pbp id isn't in the roster.
    roster = {
        p.id: p for p in db.scalars(
            select(Player).where(
                Player.season == season,
                Player.team_id.in_([c.home_team_id, c.away_team_id]),
            )
        ).all()
    }
    teams_out = per_set_lineups(events, c.away_team_id, c.home_team_id, roster, names)
    return {
        **meta, "teams": teams_out,
        "note": ("Starters = players on court at each set's first serve (from play-by-play) — usually "
                 "7: the six plus the libero, who enters pre-serve for a middle. Bench players who "
                 "entered mid-set are under 'subs'. A changed STARTING group (starter_changes) is the "
                 "real lineup-change signal — routine rotational subs are not. Libero/defensive-sub "
                 "slots are approximate where the feed omits a substitution."),
    }


# --------------------------------------------------------------------------- tool registry
# JSON-schema tool specs shared by the MCP server and the Ask box (Anthropic tool-use format).
TOOL_SPECS: list[dict] = [
    {
        "name": "leaderboard",
        "description": (
            "Rank the top players for a season by a counting or per-set stat, with optional "
            "filters. Use this for questions like 'who leads in kills', 'freshmen with the most "
            "kills', 'best passers in the Big Ten' (passing = serve receive → stat='rec_net'), "
            "'sophomore setters from Indiana with the most assists', 'top international hitters', "
            "'best players from Canada'. class_year accepts "
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
                "stat": {"type": "string", "description":
                    f"one of {sorted(_RANKABLE | _COMPUTED_STATS)}. Volleyball terms: "
                    "passer/passing/serve-receive → 'rec_net' (receptions minus reception errors), "
                    "raw parts 'retatt' (receptions) & 'rerr' (reception errors); setter/setting → "
                    "'assists'; defender/libero → 'digs'; hitter → 'kills' or 'hit_pct'; serving → "
                    "'aces' (service errors = 'serr'); blocking → 'total_blocks'"},
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
            "Team season win/loss records, set records (incl. set_pct = set win %), match win_pct, "
            "conference splits, streaks, and rankings (RPI and AVCA Coaches Poll rank). sort_by: "
            "'wins' (default), 'set_pct' (best set win %), or 'win_pct' (best match win %). Use for "
            "standings, 'best teams', 'best teams by set win %', and 'who's ranked' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "sort_by": {"type": "string", "description": "'wins' (default), 'set_pct', or 'win_pct'"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "team_stats",
        "description": (
            "Team-aggregate season stats (totals summed over the roster), ranked by sort_by. Use "
            "for 'which team has the most kills/blocks/aces' or 'best hitting team' (sort_by=hit_pct). "
            "To get ONE named team's stats (e.g. 'Bowling Green's hitting %'), pass 'team' — it "
            "returns just that team no matter how it ranks, so don't report a team as missing from "
            "a top-N list; look it up with 'team' instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "team": {"type": "string",
                         "description": "look up one specific team's aggregate stats by name"},
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
            "'conference', 'team' and 'position' filters (e.g. position='MB' for 'tallest middles'). "
            "Only players with a recorded height are counted (players_measured gives the sample "
            "size). Returns only height NUMBERS — to NAME the tallest player in a conference/team, "
            "use search_players with sort_by='height' instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "team": {"type": "string", "description": "restrict to one team by name"},
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
        "name": "transfer_impact",
        "description": (
            "Transfer players ranked by their impact at their NEW team this season. A transfer is "
            "someone who played LAST season at a DIFFERENT school (matched by durable identity — name "
            "+ hometown — since player ids don't bridge seasons). Each row includes the new team, the "
            "previous_team they came from, and season stats (kills/assists/digs/aces/blocks/points, "
            "per-set rates, and hitting %). Use for 'which transfer is having the biggest impact', "
            "'best transfer in the Big Ten', 'top transfer setter/hitter'. sort_by defaults to "
            "pts_per_set (best all-around proxy); other options: kills_per_set|assists_per_set|"
            "digs_per_set|aces_per_set|blocks_per_set|hit_pct|pts|kills|assists|digs|aces|"
            "total_blocks. min_sets drops players who've barely played. Optional conference/position "
            "filters. Detected against the immediately preceding season."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "position": {"type": "string", "description": "e.g. S, OH, MB, L/DS"},
                "sort_by": {"type": "string",
                            "description": "pts_per_set (default)|kills_per_set|assists_per_set|"
                                           "digs_per_set|aces_per_set|blocks_per_set|hit_pct|pts|"
                                           "kills|assists|digs|aces|total_blocks"},
                "min_sets": {"type": "integer", "description": "min sets played, default 10"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "team_defense",
        "description": (
            "Team defense ranked by how well each team limits its opponents' offense (aggregated "
            "from every match's box score). opp_hit_pct = the hitting percentage a team holds "
            "opponents to; lower is better, so results are best-defense-first. Use for 'best "
            "opponent hitting percentage', 'which teams force opponents into low hitting', 'best "
            "defensive team by opponent efficiency'. sort_by: opp_hit_pct|opp_kills|"
            "opp_total_attacks. Optional conference filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season": {"type": "integer"},
                "conference": {"type": "string"},
                "sort_by": {"type": "string",
                            "description": "opp_hit_pct|opp_kills|opp_total_attacks"},
                "min_games": {"type": "integer", "description": "drop teams with fewer matches"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "quality_wins",
        "description": (
            "Teams ranked by QUALITY WINS — wins over an opponent that was ranked AT THE TIME of "
            "the game (rank as of the game date, from ranking history). poll: 'avca' (Coaches Poll "
            "top 25) or 'rpi' (NCAA RPI, ranks all teams — use threshold 25/50). Use for 'best "
            "quality wins', 'who has beaten the most ranked teams', 'best wins in the Big Ten'. "
            "Note: rank history only starts from when snapshots began, so very early-season games "
            "may not count. Optional team/conference filters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "team name/short name/alias"},
                "conference": {"type": "string"},
                "poll": {"type": "string", "description": "'avca' (default) or 'rpi'"},
                "threshold": {"type": "integer", "description": "ranked cutoff, default 25"},
                "season": {"type": "integer"},
                "limit": {"type": "integer", "description": "default 25, max 100"},
            },
        },
    },
    {
        "name": "biggest_upsets",
        "description": (
            "Biggest UPSETS — games where the winner was ranked worse than the loser AT THE TIME "
            "(rank as of the game date, from ranking history). poll: 'avca' (default; wins over an "
            "AVCA Coaches Poll top-N team, winner may be unranked — the meaningful upset signal) or "
            "'rpi' (NCAA RPI ranks every team, so gap = winner rank minus loser rank, but early-"
            "season RPI is last year's rollover and unreliable until it stabilizes). Use for "
            "'biggest upsets so far', 'craziest upset this season', 'biggest upset in the Big Ten'. "
            "Rank history only starts from when snapshots began. Optional team (winner) / "
            "conference (winner) filters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "poll": {"type": "string", "description": "'avca' (default) or 'rpi'"},
                "threshold": {"type": "integer", "description": "avca ranked cutoff, default 25"},
                "min_gap": {"type": "integer", "description": "min rpi rank gap, default 1"},
                "team": {"type": "string", "description": "filter to this winner (name/alias)"},
                "conference": {"type": "string", "description": "filter to winners in this conference"},
                "season": {"type": "integer"},
                "limit": {"type": "integer", "description": "default 10, max 100"},
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
    {
        "name": "match_pbp",
        "description": (
            "Play-by-play breakdown of ONE match, resolved by team + date. Answers momentum "
            "questions a box score can't: per-set ties, lead changes, each team's biggest scoring "
            "run, the point-type mix (kills/aces/blocks/opponent errors), and the match scoring "
            "leaders (who actually put points away). Use for 'how many lead changes', 'biggest "
            "run', 'who scored the points', 'was it close/back-and-forth'. Requires a team AND a "
            "date (resolve relative dates first); pass 'opponent' for a doubleheader. Set "
            "'include_rally_log'=true only when the user wants the point-by-point sequence — it's long."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "team name/short name/alias, e.g. 'Nebraska'"},
                "date": {"type": "string", "description": "match date, YYYY-MM-DD"},
                "opponent": {"type": "string", "description": "the other team, to disambiguate a doubleheader"},
                "season": {"type": "integer"},
                "include_rally_log": {"type": "boolean",
                                      "description": "true = also return the capped point-by-point log"},
            },
            "required": ["team", "date"],
        },
    },
    {
        "name": "match_lineups",
        "description": (
            "Per-set STARTING LINEUPS and lineup changes for ONE match, resolved by team + date. "
            "Reads the play-by-play substitution log (stats.ncaa.org records subs). For each team "
            "and set: the starters (on court at the set's first serve — usually 7, the six plus the "
            "libero), the bench players who subbed in mid-set, a set-by-set diff of the starting "
            "group, and a starters_changed flag. A starter is kept even if subbed out and back in "
            "within the set (e.g. a two-setter swap). Use for 'did X change their lineup in set N', "
            "'who started each set', 'different starters', 'who came off the bench'. A changed "
            "STARTING group is the meaningful lineup change; libero/defensive-sub slots are "
            "approximate where the feed omits a sub. Requires a team AND a date (resolve relative "
            "dates first); pass 'opponent' for a doubleheader."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "team name/short name/alias, e.g. 'Wisconsin'"},
                "date": {"type": "string", "description": "match date, YYYY-MM-DD"},
                "opponent": {"type": "string", "description": "the other team, to disambiguate a doubleheader"},
                "season": {"type": "integer"},
            },
            "required": ["team", "date"],
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
    "transfer_impact": transfer_impact,
    "team_defense": team_defense,
    "quality_wins": quality_wins,
    "biggest_upsets": biggest_upsets,
    "player_game_log": player_game_log,
    "player_stats": player_stats,
    "team_schedule": team_schedule,
    "games_on_date": games_on_date,
    "match_pbp": match_pbp,
    "match_lineups": match_lineups,
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
