"""The sessions a person steers, bound to the operation registry
(docs/adr/0078). Every route here exposes one operation of
``mycelium_core.operations.sessions``; the writes are made by an agent
host's hooks through the CLI, the read by the web page and the CLI."""

from __future__ import annotations

from fastapi import APIRouter

from mycelium_api.op_routes import bind_route

router = APIRouter(prefix="/sessions", tags=["sessions"])

_ROUTES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "", "sessions.list", "list_sessions"),
    ("POST", "/open", "sessions.open", "open_session"),
    ("POST", "/report", "sessions.report", "report_session"),
    ("POST", "/reconcile", "sessions.reconcile", "reconcile_sessions"),
    ("POST", "/close", "sessions.close", "close_session"),
    ("POST", "/send", "sessions.send", "send_session_message"),
    ("GET", "/messages", "sessions.messages", "list_session_messages"),
    ("POST", "/messages/claim", "sessions.messages_claim", "claim_session_message"),
    ("POST", "/messages/outcome", "sessions.message_outcome", "record_session_message_outcome"),
)

for _method, _path, _op_id, _name in _ROUTES:
    bind_route(router, _method, _path, _op_id, name=_name)
