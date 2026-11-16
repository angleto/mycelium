"""The knowledge graph's REST routes, bound to the operation registry (ADR-0068).

Every route here exposes one operation of ``mycelium_core.operations.kg``,
which ``mycelium_mcp.tools_kg`` exposes too. For now the review inbox of
proposed facts (ADR-0071).
"""

from __future__ import annotations

from fastapi import APIRouter

from mycelium_api.op_routes import bind_route

router = APIRouter(prefix="/kg", tags=["kg"])

_ROUTES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/review/pending", "kg.review.pending", "kg_review_pending"),
    ("POST", "/review/approve", "kg.review.approve", "kg_review_approve"),
    ("POST", "/review/reject", "kg.review.reject", "kg_review_reject"),
)

for _method, _path, _op_id, _name in _ROUTES:
    bind_route(router, _method, _path, _op_id, name=_name)
