"""The session read bound to the operation registry (docs/adr/0078).

Imported at the end of ``server.py``, like the other registry-bound tools.
Only the read: the writes are made by an agent host's hooks, which see the
session's prompts, and are declared REST-only on the operation.
"""

from __future__ import annotations

from typing import Any

from mycelium_mcp.op_context import op_ctx
from mycelium_mcp.op_tools import bind_tool
from mycelium_mcp.server import mcp

TOOLS: dict[str, Any] = {
    "sessions_list": bind_tool(mcp, op_ctx, "sessions.list", name="sessions_list"),
}
