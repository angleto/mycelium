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
from pydantic import BaseModel, Field

from flow_api.deps import TenantCtx, tenant_admin_ctx, tenant_ctx
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
        rerank=body.rerank,
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


class ReindexOut(BaseModel):
    """Result of a one-shot pointer backfill. ``indexed`` is the count
    of tasks that got a pointer + blob in this call; if it equals
    ``batch_size`` there are more tasks to process and the caller
    should re-invoke (or wait for the periodic worker tick)."""

    indexed: int
    batch_size: int


class ReindexIn(BaseModel):
    batch_size: int = Field(default=200, ge=1, le=2000)


@router.post("/reindex", response_model=ReindexOut)
async def reindex(
    body: ReindexIn,
    ctx: Annotated[TenantCtx, Depends(tenant_admin_ctx)],
) -> ReindexOut:
    """Index every task that pre-dates the task-search deploy in this
    workspace. Admin-gated (the same sudo lever used by other tenant
    maintenance endpoints) because it touches every row.

    Runs the same ``_resync_task_blob`` the listener path runs on a
    fresh mutation, in one transaction, capped at ``batch_size``
    (default 200). Idempotent: tasks that already have a pointer are
    skipped by the SELECT itself.
    """
    indexed = await svc.run_pointer_backfill(ctx.session, batch_size=body.batch_size)
    return ReindexOut(indexed=indexed, batch_size=body.batch_size)
