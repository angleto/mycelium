"""The garden's REST routes, bound to the operation registry (ADR-0068).

Every route here exposes one operation of ``mycelium_core.operations.garden``,
which the MCP tools in ``mycelium_mcp.tools_garden`` expose too. The route
declares only its verb, its path and its name; the arguments, the answer, the
description and the scope come from the operation, so the two surfaces
cannot drift apart.

Member-level: every member reads the structure of their own workspace, and
the services' queries are RLS-scoped to it.
"""

from __future__ import annotations

from fastapi import APIRouter

from mycelium_api.op_routes import bind_route

router = APIRouter(prefix="/garden", tags=["garden"])

_ROUTES: tuple[tuple[str, str, str, str], ...] = (
    # sensors
    ("GET", "/health", "garden.health", "garden_health"),
    ("GET", "/health/timeseries", "garden.health.timeseries", "garden_health_timeseries"),
    ("GET", "/health/events", "garden.health.events", "garden_health_events"),
    ("GET", "/audit", "garden.events", "garden_audit"),
    # structure
    ("GET", "/graph", "garden.graph", "garden_graph"),
    ("GET", "/clusters", "garden.clusters", "garden_clusters"),
    ("GET", "/walk", "garden.walk", "garden_walk"),
    ("GET", "/link-suggestions/{note_id}", "garden.link_suggestions", "garden_link_suggestions"),
    # proposals
    ("GET", "/classify/{node_id}", "garden.classify", "garden_classify"),
    ("POST", "/apply", "garden.apply", "garden_apply"),
    ("GET", "/candidates", "garden.candidates", "garden_candidates"),
    # review of generated text
    ("GET", "/review/pending", "garden.review.pending", "garden_review_pending"),
    ("GET", "/review/rejected", "garden.review.rejected", "garden_review_rejected"),
    ("POST", "/review/approve", "garden.review.approve", "garden_review_approve"),
    ("POST", "/review/reject", "garden.review.reject", "garden_review_reject"),
    (
        "POST",
        "/review/approve-on-decision",
        "garden.review.approve_on_decision",
        "garden_review_approve_on_decision",
    ),
    (
        "POST",
        "/review/reject-on-decision",
        "garden.review.reject_on_decision",
        "garden_review_reject_on_decision",
    ),
    (
        "POST",
        "/review/restore-source",
        "garden.review.restore_source",
        "garden_review_restore_source",
    ),
    ("GET", "/review/accept-ratio", "garden.review.accept_ratio", "garden_review_accept_ratio"),
    # what the classifier learned
    ("POST", "/learning/rollback", "garden.learning.rollback", "garden_learning_rollback"),
    ("GET", "/learning/telemetry", "garden.learning.telemetry", "garden_learning_telemetry"),
)

for _method, _path, _op_id, _name in _ROUTES:
    bind_route(router, _method, _path, _op_id, name=_name)
