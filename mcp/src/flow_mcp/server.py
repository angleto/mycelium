"""MCP server skeleton.

Thin adapter over flow_core (docs/adr/0001): co-equal to the REST API,
same service layer. F0 ships only a trivial tool; domain tools land
with their phases. All tools must respect the same RBAC and
(org, project) isolation as the REST adapter.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from flow_core import __version__

mcp: FastMCP = FastMCP("flow")


@mcp.tool()
def ping() -> str:
    """Liveness probe; returns the flow-core version."""
    return f"flow-core {__version__}"
