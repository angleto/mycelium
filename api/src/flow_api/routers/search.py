"""Unified search across tasks and memory blobs.

Wraps the existing memory RRF pipeline with a kind-aware split: ``task``
kind uses the org-wide retrieve (project_id=None) and resolves blobs to
tasks via ``task_index_pointer``; ``blob`` kind keeps the per-project
predicate. Snippet is computed server-side via Postgres ``ts_headline``;
the response is typed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import SearchHit, SearchIn
from flow_core.services import task_search as svc

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=list[SearchHit])
async def search(
    body: SearchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[SearchHit]:
    hits = await svc.search_unified(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_id=ctx.project_id,
        query=body.q,
        kinds=body.kinds,
        tag_ids=body.tag_ids,
        channel_keys=body.channel_keys,
        limit=body.limit,
        include_archived=body.include_archived,
        include_deleted=body.include_deleted,
        operation_id=body.operation_id,
    )
    return [
        SearchHit(
            kind=h.kind,
            task_id=h.task_id,
            blob_id=h.blob_id,
            title=h.title,
            snippet=h.snippet,
            score=h.score,
        )
        for h in hits
    ]
