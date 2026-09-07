"""FastMCP server exposing the shared query tools over Streamable HTTP.

Mounted at ``/mcp`` on the FastAPI app (see ``vb.api.main``) and gated by a bearer token that the
admin sets in the admin panel (``app_settings.mcp_token``). External MCP clients connect to
``https://vballr.com/mcp`` with ``Authorization: Bearer <token>``.

Each tool opens its own short-lived read-only DB session. Every tool in ``vb.query.tools.TOOL_SPECS``
has a wrapper here, so the MCP server and the in-app Ask box expose the same tools and answer
identically. ``tests/test_mcp_parity.py`` fails CI if a spec ever lacks a wrapper — keep them in sync
(add the matching ``@mcp.tool()`` wrapper whenever a tool is added to the registry).
"""
from __future__ import annotations

try:  # mcp >= 2.x renamed FastMCP -> MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x fallback
    from mcp.server.fastmcp import FastMCP as _Server

from ..app_settings import KEY_MCP_TOKEN, get_setting
from ..db import SessionLocal
from ..query import tools as qt

mcp = _Server("VBallr")


def streamable_app():
    """Build the stateless Streamable-HTTP ASGI app, endpoint at the mount root.

    DNS-rebinding host validation is disabled: the endpoint is already gated by an admin-set bearer
    token (unknown callers get 401 before reaching MCP) and sits behind Caddy, which sets the Host.
    That guard targets browser-accessed localhost dev servers, not token-authenticated MCP clients.
    """
    kwargs = {"streamable_http_path": "/", "stateless_http": True}
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    except Exception:  # older/newer SDK without this knob — fall back to defaults
        pass
    return mcp.streamable_http_app(**kwargs)


def _run(name: str, **kwargs):
    db = SessionLocal()
    try:
        return qt.run_tool(db, name, {k: v for k, v in kwargs.items() if v is not None})
    finally:
        db.close()


@mcp.tool()
def leaderboard(
    stat: str = "kills", season: int | None = None, class_year: str | None = None,
    position: str | None = None, conference: str | None = None, team: str | None = None,
    state: str | None = None, hometown: str | None = None, country: str | None = None,
    international: bool = False, min_sets: float = 0, limit: int = 25,
) -> list | dict:
    """Rank the top players for a season by a stat, with optional filters.

    class_year accepts 'freshman'/'Fr', 'sophomore'/'So', 'junior'/'Jr', 'senior'/'Sr',
    'graduate'/'Gr'. 'team' limits to one team's roster (name/short name/alias). 'state' is the
    player's home state, 'country' their home country, 'international'=true keeps only players from
    outside the US. Use for questions like 'freshmen with the most kills' or 'Nebraska's top hitter'.
    """
    return _run(
        "leaderboard", stat=stat, season=season, class_year=class_year,
        position=position, conference=conference, team=team, state=state, hometown=hometown,
        country=country, international=international, min_sets=min_sets, limit=limit,
    )


@mcp.tool()
def search_players(
    query: str | None = None, season: int | None = None, position: str | None = None,
    class_year: str | None = None, conference: str | None = None, team: str | None = None,
    state: str | None = None, hometown: str | None = None, country: str | None = None,
    international: bool = False, min_height_inches: int | None = None,
    max_height_inches: int | None = None, sort_by: str = "name", limit: int = 20,
) -> list | dict:
    """Find players by name and/or roster attributes (returns player_id, team, position, class, bio).

    'team' limits to one team's roster (name/short name/alias). 'state' is the player's home state,
    'country' their home country, 'international'=true keeps only players from outside the US. Filter
    by 'min_height_inches'/'max_height_inches' (6-6 = 78) and 'sort_by'='height' (tallest first) for
    'tallest players on <team>'. Use for 'players on <team>', 'international players on <team>', or
    'setters from Texas'.
    """
    return _run(
        "search_players", query=query, season=season, position=position, class_year=class_year,
        conference=conference, team=team, state=state, hometown=hometown, country=country,
        international=international, min_height_inches=min_height_inches,
        max_height_inches=max_height_inches, sort_by=sort_by, limit=limit,
    )


@mcp.tool()
def list_teams(
    query: str | None = None, conference: str | None = None, limit: int = 50,
) -> list | dict:
    """List/search team identities (name, short name, conference) to ground fuzzy name matching.

    Use to confirm a school's exact name or resolve an abbreviation/nickname before another tool.
    """
    return _run("list_teams", query=query, conference=conference, limit=limit)


@mcp.tool()
def team_records(season: int | None = None, conference: str | None = None) -> list | dict:
    """Team season win/loss records, set records, conference splits, and streaks."""
    return _run("team_records", season=season, conference=conference)


@mcp.tool()
def player_game_log(player_id: int, season: int | None = None) -> list | dict:
    """A single player's per-match stat lines (get player_id from search_players)."""
    return _run("player_game_log", player_id=player_id, season=season)


@mcp.tool()
def player_stats(player_id: int, season: int | None = None) -> list | dict:
    """A single player's full season-to-date totals, per-set rates, and bio (needs player_id)."""
    return _run("player_stats", player_id=player_id, season=season)


@mcp.tool()
def team_stats(
    season: int | None = None, conference: str | None = None,
    team: str | None = None, sort_by: str = "kills", limit: int = 25,
) -> list | dict:
    """Team-aggregate season stats (summed over the roster), ranked by sort_by.

    sort_by: kills|assists|aces|digs|total_blocks|pts|hit_pct. Pass 'team' for ONE team's line.
    """
    return _run("team_stats", season=season, conference=conference, team=team,
                sort_by=sort_by, limit=limit)


@mcp.tool()
def team_heights(
    season: int | None = None, conference: str | None = None, team: str | None = None,
    position: str | None = None, sort_by: str = "avg_height", limit: int = 25,
) -> list | dict:
    """Per-team roster height ranked (avg height + tallest). sort_by: avg_height|max_height.

    Returns NUMBERS only; to NAME the tallest player use search_players with sort_by='height'.
    """
    return _run("team_heights", season=season, conference=conference, team=team,
                position=position, sort_by=sort_by, limit=limit)


@mcp.tool()
def game_highs(
    stat: str = "kills", season: int | None = None, conference: str | None = None,
    team: str | None = None, position: str | None = None, limit: int = 25,
) -> list | dict:
    """Best single-MATCH individual performances, ranked. stat: kills|assists|digs|aces|total_blocks|pts."""
    return _run("game_highs", stat=stat, season=season, conference=conference,
                team=team, position=position, limit=limit)


@mcp.tool()
def double_doubles(
    season: int | None = None, conference: str | None = None,
    team: str | None = None, limit: int = 25,
) -> list | dict:
    """Players ranked by double-doubles (and triple-doubles) this season. Optional conference/team."""
    return _run("double_doubles", season=season, conference=conference, team=team, limit=limit)


@mcp.tool()
def team_roster_makeup(
    season: int | None = None, conference: str | None = None,
    sort_by: str = "international", limit: int = 25,
) -> list | dict:
    """Per-team roster demographics ranked: size, international count/%, avg class year.

    sort_by: international|international_pct|youngest|oldest|size. Optional conference filter.
    """
    return _run("team_roster_makeup", season=season, conference=conference,
                sort_by=sort_by, limit=limit)


@mcp.tool()
def player_origins(
    group_by: str = "state", season: int | None = None,
    conference: str | None = None, limit: int = 25,
) -> list | dict:
    """Where players come from, grouped and counted. group_by='state' or 'country'."""
    return _run("player_origins", group_by=group_by, season=season,
                conference=conference, limit=limit)


@mcp.tool()
def team_defense(
    season: int | None = None, conference: str | None = None,
    sort_by: str = "opp_hit_pct", min_games: int = 1, limit: int = 25,
) -> list | dict:
    """Team defense ranked best-first by opponents' aggregate offense. sort_by: opp_hit_pct|opp_kills|opp_total_attacks."""
    return _run("team_defense", season=season, conference=conference,
                sort_by=sort_by, min_games=min_games, limit=limit)


@mcp.tool()
def quality_wins(
    team: str | None = None, conference: str | None = None, poll: str = "avca",
    threshold: int = 25, season: int | None = None, limit: int = 25,
) -> list | dict:
    """Teams ranked by wins over a team ranked at the time of the game. poll: 'avca' or 'rpi'."""
    return _run("quality_wins", team=team, conference=conference, poll=poll,
                threshold=threshold, season=season, limit=limit)


@mcp.tool()
def biggest_upsets(
    poll: str = "avca", threshold: int = 25, min_gap: int = 1, team: str | None = None,
    conference: str | None = None, season: int | None = None, limit: int = 10,
) -> list | dict:
    """Biggest upsets — winner ranked worse than the loser at the time. poll: 'avca' (default) or 'rpi'."""
    return _run("biggest_upsets", poll=poll, threshold=threshold, min_gap=min_gap,
                team=team, conference=conference, season=season, limit=limit)


@mcp.tool()
def team_schedule(
    team: str, season: int | None = None, upcoming_only: bool = False,
) -> list | dict:
    """A team's schedule — played results + upcoming games (opponent, date, site, result)."""
    return _run("team_schedule", team=team, season=season, upcoming_only=upcoming_only)


@mcp.tool()
def games_on_date(date: str, season: int | None = None) -> list | dict:
    """Every D1 game on a date (YYYY-MM-DD): finals + scheduled games."""
    return _run("games_on_date", date=date, season=season)


@mcp.tool()
def match_pbp(
    team: str, date: str, opponent: str | None = None,
    season: int | None = None, include_rally_log: bool = False,
) -> list | dict:
    """Play-by-play breakdown of ONE match (resolve by team + date, YYYY-MM-DD).

    Per-set ties, lead changes, each team's biggest run, point-type mix, and match scoring leaders.
    Pass 'opponent' for a doubleheader; 'include_rally_log'=true adds the (long) point-by-point log.
    """
    return _run("match_pbp", team=team, date=date, opponent=opponent,
                season=season, include_rally_log=include_rally_log)


def token_is_valid(token: str | None) -> bool:
    """True if the presented bearer token matches the admin-configured MCP token."""
    if not token:
        return False
    db = SessionLocal()
    try:
        expected = get_setting(db, KEY_MCP_TOKEN)
    finally:
        db.close()
    return bool(expected) and token == expected
