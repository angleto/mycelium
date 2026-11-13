"""The garden's MCP tools, bound to the operation registry (ADR-0068).

Imported at the end of ``server.py`` so the tools land on the same registry
the gateway dispatches. Each line names the tool, the operation it exposes,
and, where existing readers expect one, the envelope around the answer.
"""

from __future__ import annotations

from typing import Any

from mycelium_mcp.op_context import op_ctx as _op_ctx
from mycelium_mcp.op_tools import bind_tool
from mycelium_mcp.server import mcp


def _under(key: str) -> Any:
    """Wrap a list answer in ``{key: [...]}``, the shape these tools have
    always returned: a bare list gives a client nowhere to add a field later."""
    return lambda data: {key: data}


def _suggestions(data: dict[str, Any]) -> Any:
    # suggest_note_links has always answered the bare list of suggestions.
    return data["suggestions"]


_BINDINGS: tuple[tuple[str, str, Any], ...] = (
    # sensors
    ("garden_health", "garden.health", None),
    ("garden_health_timeseries", "garden.health.timeseries", _under("snapshots")),
    ("garden_health_events", "garden.health.events", _under("events")),
    ("list_events", "garden.events", _under("events")),
    # structure
    ("garden_clusters", "garden.clusters", None),
    ("graph_walk", "garden.walk.reading_set", None),
    ("suggest_note_links", "garden.link_suggestions", _suggestions),
    # proposals
    ("garden_classify", "garden.classify", None),
    ("garden_apply", "garden.apply", None),
    ("list_distillation_candidates", "garden.candidates", None),
    # review of generated text
    ("garden_review_pending", "garden.review.pending", _under("pending")),
    ("garden_review_rejected", "garden.review.rejected", _under("rejected")),
    ("garden_review_approve", "garden.review.approve", None),
    ("garden_review_reject", "garden.review.reject", None),
    ("garden_review_approve_on_decision", "garden.review.approve_on_decision", None),
    ("garden_review_reject_on_decision", "garden.review.reject_on_decision", None),
    ("note_restore_source", "garden.review.restore_source", None),
    ("garden_review_accept_ratio", "garden.review.accept_ratio", _under("models")),
    # what the classifier learned
    ("garden_learning_telemetry", "garden.learning.telemetry", None),
    ("garden_learning_rollback", "garden.learning.rollback", None),
)

#: The generated tool functions by name, for Python callers that used to
#: import the hand-written ones from ``server``.
TOOLS: dict[str, Any] = {
    name: bind_tool(mcp, _op_ctx, op_id, name=name, present=present)
    for name, op_id, present in _BINDINGS
}
