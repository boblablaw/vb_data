"""FastMCP server exposing the shared query tools over Streamable HTTP.

Mounted at ``/mcp`` on the FastAPI app (see ``vb.api.main``) and gated by a bearer token that the
admin sets in the admin panel (``app_settings.mcp_token``). External MCP clients connect to
``https://vballr.duckdns.org/mcp`` with ``Authorization: Bearer <token>``.

Each tool opens its own short-lived read-only DB session. Tools mirror ``vb.query.tools`` so the
MCP server and the in-app Ask box answer identically.
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
    position: str | None = None, conference: str | None = None,
    min_sets: float = 0, limit: int = 25,
) -> list | dict:
    """Rank the top players for a season by a stat, with optional class/position/conference filters.

    class_year accepts 'freshman'/'Fr', 'sophomore'/'So', 'junior'/'Jr', 'senior'/'Sr',
    'graduate'/'Gr'. Use for questions like 'freshmen with the most kills so far'.
    """
    return _run(
        "leaderboard", stat=stat, season=season, class_year=class_year,
        position=position, conference=conference, min_sets=min_sets, limit=limit,
    )


@mcp.tool()
def search_players(query: str, season: int | None = None, limit: int = 20) -> list | dict:
    """Find players by a name substring (returns player_id, team, position, class)."""
    return _run("search_players", query=query, season=season, limit=limit)


@mcp.tool()
def team_records(season: int | None = None, conference: str | None = None) -> list | dict:
    """Team season win/loss records, set records, conference splits, and streaks."""
    return _run("team_records", season=season, conference=conference)


@mcp.tool()
def player_game_log(player_id: int, season: int | None = None) -> list | dict:
    """A single player's per-match stat lines (get player_id from search_players)."""
    return _run("player_game_log", player_id=player_id, season=season)


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
