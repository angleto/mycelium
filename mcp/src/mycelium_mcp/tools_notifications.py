"""The notification operations bound to the operation registry (docs/adr/0068).

Imported at the end of ``server.py``, like the other registry-bound tools.
``notify_self`` lets an agent tell its person something (that it is blocked,
that a long job finished) on the person's own channels, and nowhere else
(docs/adr/0082).
"""

from __future__ import annotations

from typing import Any

from mycelium_mcp.op_context import op_ctx
from mycelium_mcp.op_tools import bind_tool
from mycelium_mcp.server import mcp

TOOLS: dict[str, Any] = {
    "notify_self": bind_tool(mcp, op_ctx, "notifications.notify_self", name="notify_self"),
}
