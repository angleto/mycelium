"""The task's MCP tools bound to the operation registry (ADR-0068).

Imported at the end of ``server.py``, like the garden's and the note's. The
pins: ``pin_task`` / ``unpin_task`` keep a task at hand for the person the
credential acts for; ``list_tasks(pinned=true)`` reads them back. And
``work_digest``: what happened on a client's or project's tasks in a window.
"""

from __future__ import annotations

from typing import Any

from mycelium_mcp.op_context import op_ctx
from mycelium_mcp.op_tools import bind_tool
from mycelium_mcp.server import mcp

TOOLS: dict[str, Any] = {
    "pin_task": bind_tool(mcp, op_ctx, "tasks.pin", name="pin_task"),
    "unpin_task": bind_tool(mcp, op_ctx, "tasks.unpin", name="unpin_task"),
    # Lean: an event carries a dozen optional fields and most are empty on
    # any one kind (a comment has no duration, a time entry no reply_to).
    "work_digest": bind_tool(
        mcp, op_ctx, "tasks.work_digest", name="work_digest", lean_answer=True
    ),
}
