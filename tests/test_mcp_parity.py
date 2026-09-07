"""Guard: the MCP server must expose a wrapper for every shared query tool.

The MCP server (``vb.mcp.server``) and the in-app Ask box both drive ``vb.query.tools.TOOL_SPECS``,
but the MCP side hand-declares one ``@mcp.tool()`` wrapper per tool. This test fails if a tool is
added to the registry without a matching MCP wrapper, so the two front-doors can't silently diverge.
No DB needed.
"""
from __future__ import annotations

from vb.mcp import server as mcp_server
from vb.query.tools import TOOL_SPECS


def test_every_query_tool_has_an_mcp_wrapper():
    spec_names = {spec["name"] for spec in TOOL_SPECS}
    missing = [n for n in sorted(spec_names) if not callable(getattr(mcp_server, n, None))]
    assert not missing, f"MCP server is missing wrappers for: {missing}"
