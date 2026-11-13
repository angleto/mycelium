"""Parity between the REST and MCP surfaces, read from the operation registry
(ADR-0068).

What used to be checked on 8 hand-picked pairs is checked here for every
operation: each one is reachable on both surfaces unless its declaration says
why not; each route and tool that binds it serves its argument model, its
read model and its scope; and every route and tool that binds nothing is
recorded in ``UNBOUND_ROUTES`` or ``UNBOUND_TOOLS`` at the bottom of this
file, lists that may only shrink.

All static: the app and the MCP registry are built in memory, no database.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import pytest
from fastapi.routing import APIRoute

import mycelium_mcp.server as mcp_server
from mycelium_api import route_scopes
from mycelium_api.app import create_app
from mycelium_api.op_routes import OPERATION_ATTR
from mycelium_core import operations as ops
from mycelium_core.mcp_scopes import HUMAN_ONLY
from mycelium_mcp import tool_scopes
from mycelium_mcp.op_tools import TOOL_OPERATIONS

#: Operations that should be reachable on a surface and are not yet, with
#: what is missing. Empty: every declared operation is bound on every
#: surface it does not declare itself absent from.
KNOWN_GAPS: dict[str, str] = {}


#: Arguments a tool declares for its own surface only (they shape the answer,
#: never which rows come back). Listed so a new one is a visible decision.
_EXTRAS: dict[str, set[str]] = {"list_notes": {"fields"}}


@pytest.fixture(scope="module")
def app() -> Any:
    return create_app()


def _bound_routes(app: Any) -> dict[tuple[str, str], tuple[str, APIRoute]]:
    out: dict[tuple[str, str], tuple[str, APIRoute]] = {}
    for r in app.routes:
        if isinstance(r, APIRoute) and (op_id := getattr(r.endpoint, OPERATION_ATTR, None)):
            for m in r.methods - {"HEAD", "OPTIONS"}:
                out[(m, r.path)] = (op_id, r)
    return out


def _unbound_routes(app: Any) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for r in app.routes:
        if isinstance(r, APIRoute) and getattr(r.endpoint, OPERATION_ATTR, None) is None:
            out |= {(m, r.path) for m in r.methods - {"HEAD", "OPTIONS"}}
    return out


def _tools() -> dict[str, Any]:
    return {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}


# --------------------------------------------------------------------------
# Every operation is reachable where it should be
# --------------------------------------------------------------------------


def test_every_operation_is_bound_on_every_surface_it_does_not_decline(app: Any) -> None:
    registry = ops.load_all()
    on_rest = {op_id for op_id, _ in _bound_routes(app).values()}
    on_mcp = set(TOOL_OPERATIONS.values())
    problems: list[str] = []
    for op_id, op in registry.items():
        want_rest = op.only != "mcp"
        want_mcp = op.only != "rest"
        if op_id in KNOWN_GAPS:
            continue
        if want_rest != (op_id in on_rest):
            problems.append(f"{op_id}: REST bound={op_id in on_rest}, declared only={op.only}")
        if want_mcp != (op_id in on_mcp):
            problems.append(f"{op_id}: MCP bound={op_id in on_mcp}, declared only={op.only}")
    assert not problems, "\n".join(problems)


def test_known_gaps_are_still_gaps(app: Any) -> None:
    """An entry that got closed must leave the list, or the list stops
    meaning anything."""
    on_rest = {op_id for op_id, _ in _bound_routes(app).values()}
    on_mcp = set(TOOL_OPERATIONS.values())
    closed = [g for g in KNOWN_GAPS if g in on_rest and g in on_mcp]
    assert not closed, f"closed gaps still listed in KNOWN_GAPS: {closed}"


def test_every_route_and_tool_is_bound_or_recorded(app: Any) -> None:
    """A new route or tool binds an operation or is added to the baseline in
    the same diff; an entry that got bound or removed leaves the baseline."""
    unbound_routes = _unbound_routes(app)
    unbound_tools = set(_tools()) - set(TOOL_OPERATIONS)
    assert not unbound_routes - UNBOUND_ROUTES, (
        "routes that bind no operation and are not in UNBOUND_ROUTES or UNBOUND_TOOLS: "
        f"{sorted(unbound_routes - UNBOUND_ROUTES)}"
    )
    assert not UNBOUND_ROUTES - unbound_routes, (
        "UNBOUND_ROUTES lists routes that are now bound or gone; delete them: "
        f"{sorted(UNBOUND_ROUTES - unbound_routes)}"
    )
    assert not unbound_tools - UNBOUND_TOOLS, (
        "tools that bind no operation and are not in UNBOUND_ROUTES or UNBOUND_TOOLS: "
        f"{sorted(unbound_tools - UNBOUND_TOOLS)}"
    )
    assert not UNBOUND_TOOLS - unbound_tools, (
        "UNBOUND_TOOLS lists tools that are now bound or gone; delete them: "
        f"{sorted(UNBOUND_TOOLS - unbound_tools)}"
    )


def test_an_operation_is_bound_at_most_once_per_surface(app: Any) -> None:
    seen: dict[str, list[str]] = {}
    for (m, p), (op_id, _) in _bound_routes(app).items():
        seen.setdefault(op_id, []).append(f"{m} {p}")
    twice = {k: v for k, v in seen.items() if len(v) > 1}
    assert not twice, f"operations bound to more than one route: {twice}"
    by_op: dict[str, list[str]] = {}
    for name, op_id in TOOL_OPERATIONS.items():
        by_op.setdefault(op_id, []).append(name)
    twice = {k: v for k, v in by_op.items() if len(v) > 1}
    assert not twice, f"operations bound to more than one tool: {twice}"


# --------------------------------------------------------------------------
# What a binding serves is what the operation declares
# --------------------------------------------------------------------------


def _expected_scope(op: ops.Operation, *, surface: str) -> object:
    if op.access is ops.META:
        return route_scopes.META if surface == "rest" else None
    return op.access


def test_a_bound_route_costs_what_its_operation_declares(app: Any) -> None:
    wrong = {
        f"{m} {p}": (route_scopes.ROUTE_SCOPES.get((m, p)), op_id)
        for (m, p), (op_id, _) in _bound_routes(app).items()
        if route_scopes.ROUTE_SCOPES.get((m, p)) != _expected_scope(ops.get(op_id), surface="rest")
    }
    assert not wrong, wrong


def test_a_bound_tool_costs_what_its_operation_declares() -> None:
    wrong = {
        name: (tool_scopes.TOOL_SCOPES.get(name), op_id)
        for name, op_id in TOOL_OPERATIONS.items()
        if tool_scopes.TOOL_SCOPES.get(name) != _expected_scope(ops.get(op_id), surface="mcp")
    }
    assert not wrong, wrong


def test_a_bound_route_serves_the_operations_models(app: Any) -> None:
    problems: list[str] = []
    for (m, p), (op_id, route) in _bound_routes(app).items():
        op = ops.get(op_id)
        if route.response_model != op.result:
            problems.append(f"{m} {p}: response_model {route.response_model} != {op.result}")
        dep = route.dependant
        args = {x.name for x in dep.query_params + dep.path_params}
        body = [b.field_info.annotation for b in dep.body_params]
        fields = set(op.params.model_fields)
        if m == "GET":
            if args != fields or body:
                problems.append(f"GET {p}: arguments {sorted(args)} != {sorted(fields)}")
            path_names = set(re.findall(r"{(\w+)}", p))
            if {x.name for x in dep.path_params} != path_names:
                problems.append(f"GET {p}: path parameters do not match the template")
        elif body != [op.params] or args:
            problems.append(f"{m} {p}: body {body} != [{op.params.__name__}]")
    assert not problems, "\n".join(problems)


def test_a_bound_tool_serves_the_operations_arguments_and_kind() -> None:
    tools = _tools()
    problems: list[str] = []
    for name, op_id in TOOL_OPERATIONS.items():
        op = ops.get(op_id)
        tool = tools[name]
        props = set(tool.parameters.get("properties", {}))
        expected = {"token", "org_id"} | set(op.params.model_fields) | _EXTRAS.get(name, set())
        if props != expected:
            problems.append(f"{name}: parameters {sorted(props)} != {sorted(expected)}")
        if not (tool.description or "").startswith(op.doc):
            problems.append(f"{name}: description is not the operation's")
        ann = tool.annotations
        if ann is None or ann.readOnlyHint != (op.kind is ops.Kind.READ):
            problems.append(f"{name}: readOnlyHint does not follow kind {op.kind.value}")
        if ann is not None and ann.destructiveHint != (op.kind is ops.Kind.DESTRUCTIVE):
            problems.append(f"{name}: destructiveHint does not follow kind {op.kind.value}")
    assert not problems, "\n".join(problems)


def test_a_read_operation_is_a_get_and_a_write_is_not(app: Any) -> None:
    wrong = [
        f"{m} {p} -> {op_id} ({ops.get(op_id).kind.value})"
        for (m, p), (op_id, _) in _bound_routes(app).items()
        if (m == "GET") != (ops.get(op_id).kind is ops.Kind.READ)
    ]
    assert not wrong, wrong


# --------------------------------------------------------------------------
# The declarations themselves
# --------------------------------------------------------------------------


def test_a_one_surface_operation_says_why() -> None:
    for op in ops.load_all().values():
        if op.only is not None:
            assert op.why and len(op.why) > 40, f"{op.id}: 'why' must explain the choice"


def test_every_operation_documents_itself() -> None:
    for op in ops.load_all().values():
        assert len(op.doc) > 40, f"{op.id} has no description for the two surfaces to publish"


def test_writing_or_judging_generated_text_stays_a_persons_act() -> None:
    """The fence of ADR-0043 now lives in one declaration, so it is asserted
    on the declaration and on both derived maps."""
    for op_id in ("garden.review.approve", "garden.review.reject"):
        assert ops.get(op_id).access is HUMAN_ONLY
    assert tool_scopes.TOOL_SCOPES["garden_review_approve"] is HUMAN_ONLY
    assert tool_scopes.TOOL_SCOPES["garden_review_reject"] is HUMAN_ONLY
    create_app()
    assert route_scopes.ROUTE_SCOPES[("POST", "/garden/review/approve")] is HUMAN_ONLY
    assert route_scopes.ROUTE_SCOPES[("POST", "/garden/review/reject")] is HUMAN_ONLY


def test_a_hand_written_scope_for_a_bound_tool_is_refused() -> None:
    """Two declarations of what a tool costs is the drift this removes: the
    binding refuses to run over one."""
    from mycelium_mcp.op_tools import bind_tool

    tool_scopes.TOOL_SCOPES["_probe_tool"] = "notes:read"
    try:
        with pytest.raises(RuntimeError, match="hand-written TOOL_SCOPES"):
            bind_tool(mcp_server.mcp, None, "garden.health", name="_probe_tool")  # type: ignore[arg-type]
    finally:
        del tool_scopes.TOOL_SCOPES["_probe_tool"]


def test_a_rest_only_operation_cannot_bind_a_tool() -> None:
    from mycelium_mcp.op_tools import bind_tool

    with pytest.raises(RuntimeError, match="REST-only"):
        bind_tool(mcp_server.mcp, None, "garden.graph", name="_probe_graph")  # type: ignore[arg-type]


def test_an_operation_with_an_unknown_scope_key_is_refused_at_import() -> None:
    with pytest.raises(ValueError, match="unknown scope keys"):

        @ops.operation(
            "garden._probe",
            domain="garden",
            kind=ops.Kind.READ,
            access="notes:reed",
            params=ops.REGISTRY["garden.health"].params,
            result=dict,
        )
        async def _probe(ctx: ops.OpCtx, p: Any) -> Any:  # pragma: no cover
            return None

    assert "garden._probe" not in ops.REGISTRY


def test_the_generated_signatures_match_the_argument_models() -> None:
    """The gateway validates a call against ``inspect.signature(tool.fn)``;
    a generated function whose signature drifted from its model would let
    an argument through that the operation then refuses, or the reverse."""
    tools = _tools()
    for name, op_id in TOOL_OPERATIONS.items():
        sig = inspect.signature(tools[name].fn)
        model = ops.get(op_id).params
        names = list(sig.parameters)[2:]
        assert names[: len(model.model_fields)] == list(model.model_fields), name
        assert set(names[len(model.model_fields) :]) == _EXTRAS.get(name, set()), name
        for fname, fi in model.model_fields.items():
            param = sig.parameters[fname]
            assert (param.default is inspect.Parameter.empty) == fi.is_required(), (name, fname)


# --------------------------------------------------------------------------
# Routes and tools not yet bound to an operation
# --------------------------------------------------------------------------
# Recorded on 2026-11-13, when the garden became the first domain on the
# registry. ``test_every_route_and_tool_is_bound_or_recorded`` requires these
# sets to equal what is actually unbound: a new route or tool binds an
# operation or is added here in the same diff, where review sees it, and an
# entry that gets bound or removed fails the test until it is deleted. A
# domain's migration is done when its entries are gone.

UNBOUND_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/.well-known/oauth-protected-resource"),
        ("GET", "/.well-known/oauth-protected-resource/{suffix:path}"),
        ("GET", "/activity/watermark"),
        ("GET", "/actors"),
        ("GET", "/admin/sdi-environment"),
        ("PUT", "/admin/sdi-environment"),
        ("PUT", "/admin/sdi-environment/intermediary"),
        ("GET", "/admin/users"),
        ("PATCH", "/admin/users/{user_id}"),
        ("GET", "/advisory/budget/{budget_id}/plan"),
        ("POST", "/advisory/errands"),
        ("POST", "/advisory/what-now"),
        ("GET", "/agent-runs"),
        ("GET", "/agent-runs/{run_id}"),
        ("POST", "/agent-runs/{run_id}/cancel"),
        ("GET", "/agent-tokens"),
        ("POST", "/agent-tokens"),
        ("DELETE", "/agent-tokens/{token_id}"),
        ("GET", "/agent/self"),
        ("GET", "/agent/workspaces"),
        ("GET", "/ai-assistants"),
        ("POST", "/ai-assistants"),
        ("GET", "/ai-assistants/connector-info"),
        ("GET", "/ai-assistants/scope-catalog"),
        ("DELETE", "/ai-assistants/{assistant_id}"),
        ("GET", "/ai-assistants/{assistant_id}"),
        ("PATCH", "/ai-assistants/{assistant_id}"),
        ("POST", "/ai-assistants/{assistant_id}/rotate"),
        ("GET", "/annotations"),
        ("GET", "/annotations/assigned"),
        ("POST", "/annotations/comment"),
        ("POST", "/annotations/comment/stream"),
        ("POST", "/annotations/suggestion"),
        ("POST", "/annotations/suggestion/stream"),
        ("PUT", "/annotations/ui-state"),
        ("DELETE", "/annotations/{annotation_id}"),
        ("GET", "/annotations/{annotation_id}"),
        ("PATCH", "/annotations/{annotation_id}"),
        ("POST", "/annotations/{annotation_id}/accept"),
        ("POST", "/annotations/{annotation_id}/assign"),
        ("POST", "/annotations/{annotation_id}/body/append"),
        ("POST", "/annotations/{annotation_id}/body/patch"),
        ("POST", "/annotations/{annotation_id}/body/prepend"),
        ("GET", "/annotations/{annotation_id}/body/raw"),
        ("POST", "/annotations/{annotation_id}/body/replace"),
        ("PATCH", "/annotations/{annotation_id}/body/stream"),
        ("POST", "/annotations/{annotation_id}/purge"),
        ("POST", "/annotations/{annotation_id}/reject"),
        ("POST", "/annotations/{annotation_id}/reopen"),
        ("POST", "/annotations/{annotation_id}/resolve"),
        ("POST", "/annotations/{annotation_id}/restore"),
        ("GET", "/annotations/{annotation_id}/revisions"),
        ("GET", "/annotations/{annotation_id}/revisions/{rev_id}"),
        ("PATCH", "/annotations/{annotation_id}/revisions/{rev_id}"),
        ("POST", "/annotations/{annotation_id}/revisions/{rev_id}/restore"),
        ("PUT", "/annotations/{annotation_id}/ui-state"),
        ("POST", "/api/v1/connectors/{provider}/{connector_id}"),
        ("GET", "/api/v1/events"),
        ("GET", "/api/v1/invoices"),
        ("POST", "/api/v1/invoices"),
        ("POST", "/api/v1/invoices/batch"),
        ("POST", "/api/v1/invoices/credit-note"),
        ("GET", "/api/v1/invoices/{invoice_id}"),
        ("GET", "/api/v1/invoices/{invoice_id}/notifications"),
        ("GET", "/api/v1/invoices/{invoice_id}/notifications/{notification_id}/xml"),
        ("GET", "/api/v1/invoices/{invoice_id}/pdf"),
        ("POST", "/api/v1/invoices/{invoice_id}/transmit"),
        ("GET", "/api/v1/invoices/{invoice_id}/xml"),
        ("GET", "/apidocs"),
        ("POST", "/attachments/capability"),
        ("POST", "/attachments/capability/write"),
        ("POST", "/attachments/stream"),
        ("DELETE", "/attachments/{attachment_id}"),
        ("GET", "/attachments/{attachment_id}/download"),
        ("POST", "/auth/device/approve"),
        ("POST", "/auth/device/authorize"),
        ("POST", "/auth/device/deny"),
        ("GET", "/auth/device/pending"),
        ("POST", "/auth/device/token"),
        ("POST", "/auth/forgot-password"),
        ("POST", "/auth/login"),
        ("POST", "/auth/login-mfa"),
        ("POST", "/auth/logout"),
        ("GET", "/auth/me"),
        ("PATCH", "/auth/me"),
        ("GET", "/auth/me/avatar"),
        ("POST", "/auth/me/avatar"),
        ("POST", "/auth/refresh"),
        ("POST", "/auth/resend-verification"),
        ("POST", "/auth/reset-password"),
        ("POST", "/auth/signup"),
        ("POST", "/auth/verify-email"),
        ("GET", "/billing/balance"),
        ("PUT", "/billing/byok-factor"),
        ("POST", "/billing/grant"),
        ("GET", "/billing/ledger"),
        ("POST", "/billing/meter"),
        ("GET", "/billing/rate-cards"),
        ("POST", "/billing/rate-cards"),
        ("PUT", "/billing/storage-rate"),
        ("GET", "/billing/usage"),
        ("GET", "/budgets"),
        ("POST", "/budgets"),
        ("DELETE", "/budgets/{budget_id}"),
        ("GET", "/budgets/{budget_id}"),
        ("PATCH", "/budgets/{budget_id}"),
        ("GET", "/budgets/{budget_id}/consumption"),
        ("GET", "/buildinfo"),
        ("GET", "/calendars"),
        ("POST", "/calendars"),
        ("GET", "/calendars/{calendar_id}/holidays"),
        ("POST", "/calendars/{calendar_id}/holidays"),
        ("DELETE", "/calendars/{calendar_id}/holidays/{day}"),
        ("POST", "/capability/text-block"),
        ("POST", "/capability/{token_id}/revoke"),
        ("GET", "/clients"),
        ("POST", "/clients"),
        ("DELETE", "/clients/{tag_id}"),
        ("PATCH", "/clients/{tag_id}"),
        ("GET", "/dependencies"),
        ("POST", "/dependencies"),
        ("DELETE", "/dependencies/{dependency_id}"),
        ("GET", "/dispatch/requests"),
        ("POST", "/dispatch/requests/{request_id}/approve"),
        ("POST", "/dispatch/requests/{request_id}/deny"),
        ("POST", "/dispatch/tick"),
        ("GET", "/email/accounts"),
        ("POST", "/email/accounts"),
        ("DELETE", "/email/accounts/{account_id}"),
        ("GET", "/email/accounts/{account_id}"),
        ("PATCH", "/email/accounts/{account_id}"),
        ("PUT", "/email/accounts/{account_id}/default-tags"),
        ("PUT", "/email/accounts/{account_id}/secret"),
        ("POST", "/email/accounts/{account_id}/send"),
        ("POST", "/email/accounts/{account_id}/sync"),
        ("GET", "/email/drafts"),
        ("POST", "/email/drafts/{job_id}/approve"),
        ("POST", "/email/drafts/{job_id}/reject"),
        ("GET", "/email/messages"),
        ("GET", "/email/messages/{message_id}"),
        ("POST", "/email/messages/{message_id}/draft"),
        ("POST", "/email/messages/{message_id}/reply"),
        ("POST", "/email/messages/{message_id}/to-note"),
        ("POST", "/email/messages/{message_id}/to-task"),
        ("GET", "/email/threads/{thread_id}"),
        ("GET", "/embedder-provider"),
        ("PUT", "/embedder-provider"),
        ("GET", "/embedder-provider/scaleway/models"),
        ("GET", "/executors"),
        ("POST", "/executors"),
        ("DELETE", "/executors/{executor_id}"),
        ("PATCH", "/executors/{executor_id}"),
        ("POST", "/export/pdf"),
        ("GET", "/graph"),
        ("GET", "/healthz"),
        ("GET", "/invoices"),
        ("POST", "/invoices"),
        ("POST", "/invoices/credit-note"),
        ("POST", "/invoices/purge-test"),
        ("POST", "/invoices/receipt"),
        ("DELETE", "/invoices/{invoice_id}"),
        ("GET", "/invoices/{invoice_id}"),
        ("PATCH", "/invoices/{invoice_id}"),
        ("POST", "/invoices/{invoice_id}/archive"),
        ("GET", "/invoices/{invoice_id}/lines"),
        ("POST", "/invoices/{invoice_id}/lines"),
        ("DELETE", "/invoices/{invoice_id}/lines/{line_id}"),
        ("PUT", "/invoices/{invoice_id}/lines/{line_id}"),
        ("GET", "/invoices/{invoice_id}/lines/{line_id}/altri-dati"),
        ("PUT", "/invoices/{invoice_id}/lines/{line_id}/altri-dati"),
        ("GET", "/invoices/{invoice_id}/notifications"),
        ("GET", "/invoices/{invoice_id}/notifications/{notification_id}/xml"),
        ("POST", "/invoices/{invoice_id}/paid"),
        ("GET", "/invoices/{invoice_id}/pdf"),
        ("GET", "/invoices/{invoice_id}/preview"),
        ("POST", "/invoices/{invoice_id}/reopen"),
        ("POST", "/invoices/{invoice_id}/restore"),
        ("POST", "/invoices/{invoice_id}/transmit"),
        ("POST", "/invoices/{invoice_id}/trash"),
        ("POST", "/invoices/{invoice_id}/unarchive"),
        ("GET", "/invoices/{invoice_id}/xml"),
        ("GET", "/issuer-profiles"),
        ("POST", "/issuer-profiles"),
        ("GET", "/issuer-profiles/{issuer_profile_id}/api-keys"),
        ("POST", "/issuer-profiles/{issuer_profile_id}/api-keys"),
        ("DELETE", "/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}"),
        ("PUT", "/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}/allowlist"),
        ("POST", "/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}/rotate"),
        ("GET", "/issuer-profiles/{issuer_profile_id}/payment-connectors"),
        ("POST", "/issuer-profiles/{issuer_profile_id}/payment-connectors"),
        ("DELETE", "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"),
        ("PATCH", "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"),
        (
            "DELETE",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/api-key",
        ),
        (
            "POST",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/assign-customer",
        ),
        (
            "GET",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/deliveries",
        ),
        (
            "POST",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/discard-dry-run",
        ),
        ("GET", "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/events"),
        (
            "GET",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/events/{event_id}/dry-run-xml",
        ),
        (
            "GET",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/events/{event_id}/payload",
        ),
        (
            "POST",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/events/{event_id}/promote",
        ),
        (
            "POST",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/events/{event_id}/recompose",
        ),
        (
            "POST",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/events/{event_id}/reshoot-dry-run-xml",
        ),
        (
            "POST",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/events/{event_id}/retry",
        ),
        (
            "POST",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/rotate-api-key",
        ),
        (
            "POST",
            "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/rotate-signing-secret",
        ),
        ("GET", "/issuer-profiles/{issuer_profile_id}/webhook-endpoints"),
        ("POST", "/issuer-profiles/{issuer_profile_id}/webhook-endpoints"),
        ("DELETE", "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}"),
        ("PATCH", "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}"),
        ("GET", "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}/deliveries"),
        (
            "POST",
            "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}/rotate-secret",
        ),
        ("DELETE", "/issuer-profiles/{profile_id}"),
        ("GET", "/issuer-profiles/{profile_id}"),
        ("PATCH", "/issuer-profiles/{profile_id}"),
        ("PUT", "/issuer-profiles/{profile_id}/conservation"),
        ("GET", "/issuer-profiles/{profile_id}/counters"),
        ("PUT", "/issuer-profiles/{profile_id}/counters/{series}/{year}"),
        ("POST", "/issuer-profiles/{profile_id}/default"),
        ("DELETE", "/issuer-profiles/{profile_id}/logo"),
        ("GET", "/issuer-profiles/{profile_id}/logo"),
        ("POST", "/issuer-profiles/{profile_id}/logo"),
        ("DELETE", "/issuer-profiles/{profile_id}/mandate"),
        ("GET", "/issuer-profiles/{profile_id}/mandate"),
        ("POST", "/issuer-profiles/{profile_id}/mandate"),
        ("GET", "/issuer-profiles/{profile_id}/mandates"),
        ("GET", "/llm-provider"),
        ("PUT", "/llm-provider"),
        ("GET", "/llm-provider/scaleway/models"),
        ("GET", "/lookup/{prefix}"),
        ("POST", "/memory/blobs"),
        ("DELETE", "/memory/blobs/{blob_id}"),
        ("GET", "/memory/blobs/{blob_id}"),
        ("POST", "/memory/blobs/{blob_id}/tags"),
        ("DELETE", "/memory/blobs/{blob_id}/tags/{tag_id}"),
        ("GET", "/memory/channels"),
        ("POST", "/memory/channels"),
        ("DELETE", "/memory/channels/{channel_id}"),
        ("PATCH", "/memory/channels/{channel_id}"),
        ("POST", "/memory/consolidate"),
        ("POST", "/memory/erase"),
        ("POST", "/memory/migrate-embeddings"),
        ("GET", "/memory/migration-status"),
        ("POST", "/memory/rechunk"),
        ("POST", "/memory/recompute-tier"),
        ("POST", "/memory/search"),
        ("GET", "/memory/status"),
        ("POST", "/mfa/activate"),
        ("POST", "/mfa/disable"),
        ("POST", "/mfa/setup"),
        ("GET", "/mfa/status"),
        ("POST", "/notes"),
        ("POST", "/notes/command"),
        ("POST", "/notes/conversations"),
        ("GET", "/notes/links"),
        ("POST", "/notes/merge"),
        ("POST", "/notes/quick-create"),
        ("POST", "/notes/synthesize"),
        ("PATCH", "/notes/{note_id}"),
        ("POST", "/notes/{note_id}/append"),
        ("POST", "/notes/{note_id}/archive"),
        ("GET", "/notes/{note_id}/attachments"),
        ("POST", "/notes/{note_id}/attachments"),
        ("GET", "/notes/{note_id}/checklist"),
        ("POST", "/notes/{note_id}/checklist"),
        ("DELETE", "/notes/{note_id}/checklist/{item_id}"),
        ("PATCH", "/notes/{note_id}/checklist/{item_id}"),
        ("POST", "/notes/{note_id}/checklist:clear_done"),
        ("POST", "/notes/{note_id}/checklist:reorder"),
        ("POST", "/notes/{note_id}/delete"),
        ("POST", "/notes/{note_id}/derive-task"),
        ("POST", "/notes/{note_id}/distill"),
        ("POST", "/notes/{note_id}/edit-session/seal"),
        ("POST", "/notes/{note_id}/erase"),
        ("DELETE", "/notes/{note_id}/links"),
        ("GET", "/notes/{note_id}/links"),
        ("POST", "/notes/{note_id}/links"),
        ("POST", "/notes/{note_id}/maturity"),
        ("POST", "/notes/{note_id}/messages"),
        ("POST", "/notes/{note_id}/parts"),
        ("PUT", "/notes/{note_id}/parts/order"),
        ("POST", "/notes/{note_id}/parts/stream"),
        ("PUT", "/notes/{note_id}/parts/ui-state"),
        ("DELETE", "/notes/{note_id}/parts/{part_id}"),
        ("PATCH", "/notes/{note_id}/parts/{part_id}"),
        ("POST", "/notes/{note_id}/parts/{part_id}/append"),
        ("POST", "/notes/{note_id}/parts/{part_id}/body/patch"),
        ("GET", "/notes/{note_id}/parts/{part_id}/body/raw"),
        ("PUT", "/notes/{note_id}/parts/{part_id}/body/stream"),
        ("POST", "/notes/{note_id}/parts/{part_id}/prepend"),
        ("POST", "/notes/{note_id}/parts/{part_id}/replace"),
        ("POST", "/notes/{note_id}/parts/{part_id}/restore"),
        ("POST", "/notes/{note_id}/parts/{part_id}/trash"),
        ("PUT", "/notes/{note_id}/parts/{part_id}/ui-state"),
        ("GET", "/notes/{note_id}/parts:trashed"),
        ("POST", "/notes/{note_id}/promote"),
        ("POST", "/notes/{note_id}/protect"),
        ("POST", "/notes/{note_id}/restore"),
        ("GET", "/notes/{note_id}/revisions"),
        ("GET", "/notes/{note_id}/revisions/{rev_id}"),
        ("PATCH", "/notes/{note_id}/revisions/{rev_id}"),
        ("POST", "/notes/{note_id}/revisions/{rev_id}/restore"),
        ("POST", "/notes/{note_id}/tags"),
        ("DELETE", "/notes/{note_id}/tags/{tag_id}"),
        ("DELETE", "/notes/{note_id}/task-links"),
        ("POST", "/notes/{note_id}/task-links"),
        ("POST", "/notes/{note_id}/transcribe"),
        ("GET", "/notes/{note_id}/turns"),
        ("POST", "/notes/{note_id}/unarchive"),
        ("POST", "/notes/{note_id}/unprotect"),
        ("GET", "/notifications"),
        ("POST", "/notifications/dispatch"),
        ("GET", "/notifications/prefs"),
        ("PUT", "/notifications/prefs"),
        ("POST", "/notifications/push/subscribe"),
        ("POST", "/notifications/push/unsubscribe"),
        ("GET", "/notifications/push/vapid-public-key"),
        ("POST", "/notifications/recurrences"),
        ("POST", "/notifications/recurrences/spawn-due"),
        ("POST", "/notifications/reminders/scan"),
        ("DELETE", "/notifications/{notification_id}"),
        ("GET", "/oauth/authorize"),
        ("GET", "/oauth/google/callback"),
        ("GET", "/oauth/google/start"),
        ("POST", "/oauth/token"),
        ("GET", "/payment-connectors/vocabulary"),
        ("GET", "/projects"),
        ("POST", "/projects"),
        ("PATCH", "/projects/{project_tag_id}/workflow"),
        ("DELETE", "/projects/{tag_id}"),
        ("PATCH", "/projects/{tag_id}"),
        ("GET", "/readyz"),
        ("POST", "/received-invoices/{received_invoice_id}/esito-committente"),
        ("GET", "/schedule"),
        ("POST", "/schedule/recompute"),
        ("GET", "/schedule/{task_id}"),
        ("POST", "/search"),
        ("POST", "/search/click"),
        ("POST", "/search/reindex"),
        ("GET", "/tags"),
        ("POST", "/tags"),
        ("PATCH", "/tags/{tag_id}"),
        ("PUT", "/tags/{tag_id}/scope"),
        ("GET", "/task-relations"),
        ("POST", "/task-relations"),
        ("DELETE", "/task-relations/{relation_id}"),
        ("GET", "/tasks"),
        ("POST", "/tasks"),
        ("GET", "/tasks/leases"),
        ("GET", "/tasks/leases/last-handoff"),
        ("POST", "/tasks/leases/pull"),
        ("GET", "/tasks/workers"),
        ("POST", "/tasks/workers"),
        ("POST", "/tasks/workers/{worker_id}/close"),
        ("GET", "/tasks/{task_id}"),
        ("PATCH", "/tasks/{task_id}"),
        ("POST", "/tasks/{task_id}/archive"),
        ("POST", "/tasks/{task_id}/assignees"),
        ("DELETE", "/tasks/{task_id}/assignees/{user_id}"),
        ("GET", "/tasks/{task_id}/attachments"),
        ("POST", "/tasks/{task_id}/attachments"),
        ("GET", "/tasks/{task_id}/checklist"),
        ("POST", "/tasks/{task_id}/checklist"),
        ("DELETE", "/tasks/{task_id}/checklist/{item_id}"),
        ("PATCH", "/tasks/{task_id}/checklist/{item_id}"),
        ("POST", "/tasks/{task_id}/checklist:clear_done"),
        ("POST", "/tasks/{task_id}/checklist:reorder"),
        ("POST", "/tasks/{task_id}/claim"),
        ("GET", "/tasks/{task_id}/comments"),
        ("POST", "/tasks/{task_id}/comments"),
        ("POST", "/tasks/{task_id}/decline"),
        ("POST", "/tasks/{task_id}/delete"),
        ("POST", "/tasks/{task_id}/description/append"),
        ("POST", "/tasks/{task_id}/description/patch"),
        ("POST", "/tasks/{task_id}/description/prepend"),
        ("GET", "/tasks/{task_id}/description/raw"),
        ("POST", "/tasks/{task_id}/description/replace"),
        ("PUT", "/tasks/{task_id}/description/stream"),
        ("POST", "/tasks/{task_id}/edit-session/seal"),
        ("GET", "/tasks/{task_id}/handoffs"),
        ("GET", "/tasks/{task_id}/leases"),
        ("POST", "/tasks/{task_id}/leases"),
        ("POST", "/tasks/{task_id}/leases/preempt"),
        ("POST", "/tasks/{task_id}/leases/release"),
        ("POST", "/tasks/{task_id}/leases/renew"),
        ("POST", "/tasks/{task_id}/note"),
        ("DELETE", "/tasks/{task_id}/note-links"),
        ("GET", "/tasks/{task_id}/note-links"),
        ("POST", "/tasks/{task_id}/note-links"),
        ("POST", "/tasks/{task_id}/notes"),
        ("POST", "/tasks/{task_id}/offer"),
        ("GET", "/tasks/{task_id}/participants"),
        ("POST", "/tasks/{task_id}/participants"),
        ("DELETE", "/tasks/{task_id}/participants/{identity_id}"),
        ("GET", "/tasks/{task_id}/reminders"),
        ("POST", "/tasks/{task_id}/reminders"),
        ("DELETE", "/tasks/{task_id}/reminders/{reminder_id}"),
        ("POST", "/tasks/{task_id}/restore"),
        ("GET", "/tasks/{task_id}/revisions"),
        ("GET", "/tasks/{task_id}/revisions/{rev_id}"),
        ("PATCH", "/tasks/{task_id}/revisions/{rev_id}"),
        ("POST", "/tasks/{task_id}/revisions/{rev_id}/restore"),
        ("POST", "/tasks/{task_id}/run"),
        ("PATCH", "/tasks/{task_id}/schedule"),
        ("POST", "/tasks/{task_id}/state"),
        ("GET", "/tasks/{task_id}/states"),
        ("POST", "/tasks/{task_id}/tags"),
        ("DELETE", "/tasks/{task_id}/tags/{tag_id}"),
        ("POST", "/tasks/{task_id}/unarchive"),
        ("DELETE", "/telegram/link"),
        ("POST", "/telegram/link/request"),
        ("GET", "/telegram/link/status"),
        ("POST", "/telegram/webhook/{secret}"),
        ("GET", "/time/entries"),
        ("POST", "/time/entries"),
        ("GET", "/time/entries.csv"),
        ("DELETE", "/time/entries/{entry_id}"),
        ("GET", "/time/entries/{entry_id}"),
        ("PATCH", "/time/entries/{entry_id}"),
        ("POST", "/time/pause"),
        ("GET", "/time/report"),
        ("GET", "/time/report.csv"),
        ("GET", "/time/report/by-task"),
        ("GET", "/time/report/daily"),
        ("GET", "/time/report/daily.csv"),
        ("POST", "/time/resume"),
        ("GET", "/time/running"),
        ("POST", "/time/start"),
        ("POST", "/time/stop"),
        ("PUT", "/users/{user_id}/calendar"),
        ("GET", "/workflows"),
        ("POST", "/workflows"),
        ("POST", "/workflows/import"),
        ("DELETE", "/workflows/{workflow_id}"),
        ("PATCH", "/workflows/{workflow_id}"),
        ("POST", "/workflows/{workflow_id}/default"),
        ("GET", "/workflows/{workflow_id}/export"),
        ("POST", "/workflows/{workflow_id}/import"),
        ("GET", "/workflows/{workflow_id}/states"),
        ("GET", "/workflows/{workflow_id}/transitions"),
        ("GET", "/workspaces"),
        ("POST", "/workspaces"),
        ("GET", "/workspaces/me"),
        ("PATCH", "/workspaces/me"),
        ("GET", "/workspaces/me/members"),
        ("POST", "/workspaces/me/members"),
        ("DELETE", "/workspaces/me/members/{user_id}"),
        ("PATCH", "/workspaces/me/members/{user_id}"),
        ("PATCH", "/workspaces/me/settings"),
        ("POST", "/workspaces/me/trash/empty"),
        ("DELETE", "/workspaces/{workspace_id}"),
        ("POST", "/workspaces/{workspace_id}/archive"),
        ("POST", "/workspaces/{workspace_id}/unarchive"),
    }
)

UNBOUND_TOOLS: frozenset[str] = frozenset(
    {
        "accept_suggestion",
        "accept_suggestion_on_decision",
        "add_annotation",
        "add_checklist_item",
        "add_comment",
        "add_comment_instructions",
        "add_dependency",
        "add_holiday",
        "add_invoice_line",
        "add_note_part",
        "add_note_part_instructions",
        "add_note_tag",
        "add_task_participant",
        "add_task_relation",
        "add_task_tag",
        "add_time_entry",
        "agent_run_cancel",
        "agent_run_get",
        "agent_run_start",
        "agent_runs_list",
        "append_message",
        "append_note_part",
        "append_to_comment",
        "append_to_note",
        "append_to_task_description",
        "approve_email_draft",
        "archive_note",
        "archive_task",
        "assign_annotation",
        "assign_task",
        "billing_balance",
        "budget_consumption",
        "check_item",
        "clear_done",
        "clear_note_project",
        "count_annotations",
        "count_tasks",
        "create_budget",
        "create_calendar",
        "create_client",
        "create_email_account",
        "create_invoice",
        "create_note",
        "create_project",
        "create_recurrence",
        "create_tag",
        "create_task",
        "create_task_note",
        "create_workflow",
        "delete_annotation",
        "delete_attachment",
        "delete_budget",
        "delete_comment",
        "delete_note",
        "delete_note_part",
        "delete_task",
        "delete_time_entry",
        "delete_workflow",
        "derive_task_from_note",
        "dispatch_approve",
        "dispatch_deny",
        "dispatch_notifications",
        "dispatch_requests_list",
        "dispatch_tick",
        "distill_note",
        "download_attachment_capability",
        "draft_email_reply",
        "edit_annotation",
        "edit_annotation_body_instructions",
        "email_thread",
        "email_to_note",
        "email_to_task",
        "errands",
        "executor_create",
        "executor_delete",
        "executor_update",
        "executors_list",
        "extract_cluster_pattern",
        "gdpr_erase_note",
        "get_comment",
        "get_comment_revision",
        "get_invoice",
        "get_invoice_xml",
        "get_note_part",
        "get_note_part_body_capability",
        "get_note_revision",
        "get_or_create_task_note",
        "get_schedule",
        "get_tag",
        "get_task",
        "get_task_revision",
        "get_text_block_capability",
        "get_time_entry",
        "grant_credits",
        "graph",
        "graph_focus_context",
        "help",
        "ingest_sdi_receipt",
        "invoice_credit_note",
        "kg_entities",
        "kg_extract",
        "kg_neighbors",
        "link_notes",
        "list_annotations",
        "list_assigned_annotations",
        "list_attachments",
        "list_budgets",
        "list_calendars",
        "list_checklist",
        "list_clients",
        "list_comment_revisions",
        "list_comments",
        "list_dependencies",
        "list_email_accounts",
        "list_email_drafts",
        "list_email_messages",
        "list_holidays",
        "list_invoice_lines",
        "list_invoices",
        "list_issuer_profiles",
        "list_note_links",
        "list_note_revisions",
        "list_note_task_links",
        "list_projects",
        "list_rate_cards",
        "list_running_timers",
        "list_schedule",
        "list_tags",
        "list_task_participants",
        "list_task_relations",
        "list_task_revisions",
        "list_tasks",
        "list_time_entries",
        "list_trashed_comments",
        "list_trashed_note_parts",
        "list_turns",
        "list_usage",
        "list_workflows",
        "memory_attach_tag",
        "memory_channel_create",
        "memory_channel_delete",
        "memory_channel_update",
        "memory_channels_list",
        "memory_consolidate",
        "memory_delete_blob",
        "memory_detach_tag",
        "memory_erase",
        "memory_get_blob",
        "memory_migrate",
        "memory_migration_status",
        "memory_recompute_tiers",
        "memory_search",
        "memory_status",
        "memory_write",
        "merge_notes",
        "meter_usage",
        "move_note_to_project",
        "move_task_to_project",
        "patch_note_part_body_capability",
        "patch_text_block_capability",
        "pause_timer",
        "ping",
        "prepend_note_part",
        "prepend_to_comment",
        "prepend_to_task_description",
        "prioritize_within_budget",
        "promote_note_to_task",
        "propose_suggestion",
        "propose_suggestion_instructions",
        "protect_note",
        "purge_comment",
        "recompute_schedule",
        "record_task_artifact",
        "reject_email_draft",
        "reject_suggestion",
        "remove_dependency",
        "remove_holiday",
        "remove_item",
        "remove_note_tag",
        "remove_task_participant",
        "remove_task_relation",
        "remove_task_tag",
        "reopen_annotation",
        "reorder_note_parts",
        "replace_in_comment",
        "replace_in_note_part",
        "replace_in_task_description",
        "reply_email",
        "resolve_annotation",
        "resolve_prefix",
        "restore_comment",
        "restore_comment_revision",
        "restore_note",
        "restore_note_part",
        "restore_note_revision",
        "restore_task",
        "restore_task_revision",
        "resume_timer",
        "run_command",
        "scan_reminders",
        "search",
        "send_email",
        "set_default_workflow",
        "set_email_auto_draft_replies",
        "set_email_default_tags",
        "set_email_ingest_to_memory",
        "set_invoice_line_altri_dati",
        "set_issuer_profile",
        "set_note_client",
        "set_note_maturity",
        "set_note_part_body_capability",
        "set_note_part_body_instructions",
        "set_notification_pref",
        "set_project_workflow",
        "set_tag_scope",
        "set_task_assignee",
        "set_task_client",
        "set_task_owner",
        "set_task_schedule",
        "set_task_state",
        "set_text_block_capability",
        "set_user_calendar",
        "spawn_due_recurrences",
        "start_conversation_session",
        "start_task_on_note",
        "start_timer",
        "stop_timer",
        "sync_email_account",
        "synthesize_season",
        "synthesize_speech",
        "task_claim",
        "task_decline",
        "task_handoffs_list",
        "task_lease_acquire",
        "task_lease_preempt",
        "task_lease_release",
        "task_lease_renew",
        "task_leases_list",
        "task_offer",
        "task_pull",
        "task_workflow",
        "time_report",
        "time_report_by_task",
        "transcribe_note",
        "transmit_invoice",
        "trash_note_part",
        "unassign_task",
        "uncheck_item",
        "unlink_note_task",
        "unlink_notes",
        "update_budget",
        "update_client",
        "update_comment",
        "update_note",
        "update_note_part",
        "update_project",
        "update_tag",
        "update_task",
        "update_time_entry",
        "update_workflow",
        "upload_attachment",
        "upload_attachment_capability",
        "upload_attachment_instructions",
        "upsert_rate_card",
        "what_can_i_do_now",
        "whoami",
        "worker_close",
        "worker_open",
        "workers_list",
        "workflow_states",
        "workflow_transitions",
    }
)
