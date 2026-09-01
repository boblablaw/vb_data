"""Shared, read-only stat query tools used by BOTH the MCP server and the in-app Ask box."""
from .tools import TOOL_SPECS, run_tool

__all__ = ["TOOL_SPECS", "run_tool"]
