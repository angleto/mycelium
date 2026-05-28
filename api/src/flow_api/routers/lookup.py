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

from flow_api.deps import TenantCtx, tenant_ctx
from flow_core.services import lookup as svc

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


class LookupOut(BaseModel):
    prefix: str
    matches: list[LookupMatchOut]


_ROUTE = {"task": "/tasks/{}", "note": "/notes/{}"}


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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    kinds: Annotated[str | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
    include_deleted: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> LookupOut:
    matches = await svc.resolve_prefix(
        ctx.session,
        prefix=prefix,
        kinds=_kinds_csv(kinds),
        include_archived=include_archived,
        include_deleted=include_deleted,
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
                route_url=_ROUTE[m.kind].format(m.id),
            )
            for m in matches
        ],
    )
