"""Connections between notes and tasks, bound to the operation registry
(ADR-0068, ADR-0077). Every route here exposes one operation of
``mycelium_core.operations.connections``, which the MCP surface exposes
too."""

from __future__ import annotations

from fastapi import APIRouter

from mycelium_api.op_routes import bind_route

router = APIRouter(prefix="/connections", tags=["connections"])

_ROUTES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "", "connections.list", "list_connections"),
    ("POST", "/connect", "connections.connect", "connect_items"),
    ("POST", "/disconnect", "connections.disconnect", "disconnect_items"),
)

for _method, _path, _op_id, _name in _ROUTES:
    bind_route(router, _method, _path, _op_id, name=_name)
