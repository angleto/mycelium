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
    ("GET", "/references/backlinks", "garden.references.backlinks", "garden_written_backlinks"),
    ("POST", "/references/choose", "garden.references.choose", "garden_reference_choose"),
    ("POST", "/link-proposals", "garden.link_proposals.submit", "garden_link_proposal_submit"),
    ("GET", "/link-proposals", "garden.link_proposals.queue", "garden_link_proposal_queue"),
    ("POST", "/link-proposals/shown", "garden.link_proposals.shown", "garden_link_proposal_shown"),
    (
        "POST",
        "/link-proposals/accept",
        "garden.link_proposals.accept",
        "garden_link_proposal_accept",
    ),
    (
        "POST",
        "/link-proposals/reject",
        "garden.link_proposals.reject",
        "garden_link_proposal_reject",
    ),
    (
        "GET",
        "/link-proposals/precision",
        "garden.link_proposals.precision",
        "garden_link_proposer_precision",
    ),
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
    # every kind of proposal in one listing (ADR-0075)
    ("GET", "/proposals", "garden.proposals.list", "garden_proposals"),
    ("POST", "/proposals/decide", "garden.proposals.decide", "garden_proposal_decide"),
    ("POST", "/proposals/reopen", "garden.proposals.reopen", "garden_proposal_reopen"),
    ("GET", "/notes/{note_id}/history", "garden.note_history", "garden_note_history"),
    ("POST", "/synthesize/pattern", "garden.synthesize.pattern", "garden_synthesize_pattern"),
    ("POST", "/synthesize/season", "garden.synthesize.season", "garden_synthesize_season"),
    # decisions handed over to assistants (ADR-0076 phase B)
    ("GET", "/delegation/policy", "garden.delegation.policy", "garden_delegation_policy"),
    ("POST", "/delegation/decide", "garden.delegation.decide", "garden_delegation_decide"),
    (
        "GET",
        "/delegation/decisions",
        "garden.delegation.decisions",
        "garden_delegation_decisions",
    ),
    ("POST", "/delegation/revert", "garden.delegation.revert", "garden_delegation_revert"),
    ("GET", "/delegation/stats", "garden.delegation.stats", "garden_delegation_stats"),
    # Static segments before the parametrised ones: ``/review/nodes/...``
    # and ``/review/by-source/...`` carry their own prefix, so no id can
    # shadow ``/review/pending`` or ``/review/rejected``.
    ("GET", "/review/nodes/{note_id}", "garden.review.read", "garden_review_read"),
    (
        "GET",
        "/review/nodes/{note_id}/sources/{source_note_id}",
        "garden.review.read_source",
        "garden_review_read_source",
    ),
    (
        "GET",
        "/review/by-source/{note_id}",
        "garden.review.for_source",
        "garden_review_for_source",
    ),
    ("POST", "/review/reopen", "garden.review.reopen", "garden_review_reopen"),
    ("POST", "/review/check", "garden.review.check", "garden_review_check"),
    ("POST", "/review/revise", "garden.review.revise", "garden_review_revise"),
    ("POST", "/review/remove", "garden.review.remove", "garden_review_remove"),
    # what the classifier learned
    ("POST", "/learning/rollback", "garden.learning.rollback", "garden_learning_rollback"),
    ("GET", "/learning/telemetry", "garden.learning.telemetry", "garden_learning_telemetry"),
)

for _method, _path, _op_id, _name in _ROUTES:
    bind_route(router, _method, _path, _op_id, name=_name)
