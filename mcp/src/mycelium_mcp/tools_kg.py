"""The knowledge graph's MCP tools bound to the operation registry (ADR-0068).

Imported at the end of ``server.py``, like the garden's. For now the review
inbox of proposed facts (ADR-0071); the older kg tools are still hand-written
in ``server.py``.
"""

from __future__ import annotations

from typing import Any

from mycelium_mcp.op_context import op_ctx
from mycelium_mcp.op_tools import bind_tool
from mycelium_mcp.server import mcp

TOOLS: dict[str, Any] = {
    "kg_review_pending": bind_tool(mcp, op_ctx, "kg.review.pending", name="kg_review_pending"),
    "kg_review_approve": bind_tool(mcp, op_ctx, "kg.review.approve", name="kg_review_approve"),
    "kg_review_reject": bind_tool(mcp, op_ctx, "kg.review.reject", name="kg_review_reject"),
}
