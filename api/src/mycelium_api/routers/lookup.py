"""``GET /lookup/{prefix}`` — resolve a UUID prefix to one or more
entities (task / note). Powers the markdown prefix chip in the SPA
renderer and the short-URL routes ``/n/<prefix>`` and ``/t/<prefix>``.

Tenant-scoped via ``tenant_ctx``; the resolver inherits RLS so it can
only see entities the caller is entitled to see. Archived / deleted
rows are excluded by default; pass the matching query flag to opt in
(used by the Trash and archive views).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_core.services import lookup as svc
from mycelium_core.services.note_effective import is_review_gated

router = APIRouter(prefix="/lookup", tags=["lookup"])


class LookupMatchOut(BaseModel):
    kind: str
    id: str
    title: str | None
    state_name: str | None
    is_terminal: bool | None
    is_archived: bool
    is_deleted: bool
    route_url: str
    # 'proposed' or 'declined' for a garden proposal (its route is the review
    # reading view), 'approved' for an approved one, null otherwise.
    review_state: str | None = None


class LookupOut(BaseModel):
    prefix: str
    matches: list[LookupMatchOut]


_ROUTE = {"task": "/tasks/{}", "note": "/notes/{}"}
#: A proposal is not openable on /notes; it is read and decided here.
_REVIEW_ROUTE = "/garden/review/{}"


def _kinds_csv(value: str | None) -> tuple[str, ...]:
    """Parse the ``?kinds=`` query: comma-separated, defaults to
    ``task,note``, unknown kinds are silently dropped (so the SPA can
    forward an enum it doesn't fully recognise without 400-ing)."""
    if not value:
        return ("task", "note")
    out = tuple(k.strip().lower() for k in value.split(",") if k.strip())
    valid = tuple(k for k in out if k in ("task", "note"))
    return valid or ("task", "note")


@router.get("/{prefix}", response_model=LookupOut)
async def lookup_prefix(
    prefix: str,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    kinds: Annotated[str | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
    include_deleted: Annotated[bool, Query()] = False,
    include_gated: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> LookupOut:
    """Resolve an id prefix. ``include_gated`` is for name resolution only
    (a pasted code, a short URL, the command palette): it finds a garden
    proposal still awaiting a decision, or declined, and routes it to the
    review reading view (ADR-0075), where it is read and decided. A list of
    things to link to (the mention picker) never asks for it."""
    matches = await svc.resolve_prefix(
        ctx.session,
        prefix=prefix,
        kinds=_kinds_csv(kinds),
        include_archived=include_archived,
        include_deleted=include_deleted,
        include_gated=include_gated,
        limit=limit,
    )
    return LookupOut(
        prefix=svc.normalise_prefix(prefix),
        matches=[
            LookupMatchOut(
                kind=m.kind,
                id=str(m.id),
                title=m.title,
                state_name=m.state_name,
                is_terminal=m.is_terminal,
                is_archived=m.is_archived,
                is_deleted=m.is_deleted,
                route_url=(
                    _REVIEW_ROUTE.format(m.id)
                    if m.kind == "note" and is_review_gated(m.review_state)
                    else _ROUTE[m.kind].format(m.id)
                ),
                review_state=m.review_state,
            )
            for m in matches
        ],
    )
