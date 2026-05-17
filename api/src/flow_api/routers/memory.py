"""Memory router: write, hybrid search (retrieval-as-tool), get, GDPR
erase, consolidate, tier recompute. Thin adapter (docs/adr/0001, 0005,
0007, 0016, FR-8). The (org, project) predicate is enforced in the
service; the embedder is injected (fake in tests)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    ErasedOut,
    MemoryBlobOut,
    MemoryConsolidateIn,
    MemoryEraseIn,
    MemoryHitOut,
    MemorySearchIn,
    MemoryWriteIn,
    TierCountsOut,
)
from flow_core.models.memory_blob import MemoryBlob
from flow_core.services import memory as svc

router = APIRouter(prefix="/memory", tags=["memory"])


def _blob_out(b: MemoryBlob) -> MemoryBlobOut:
    return MemoryBlobOut(
        id=b.id,
        project_id=b.project_id,
        namespace=b.namespace,
        tier=b.tier,
        text=b.text,
        summary=b.summary,
        model_id=b.model_id,
        dim=b.dim,
        access_count=b.access_count,
        cluster_id=b.cluster_id,
    )


@router.post("/blobs", response_model=MemoryBlobOut)
async def write_blob(
    body: MemoryWriteIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> MemoryBlobOut:
    blob = await svc.write_blob(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_id=body.project_id,
        text_body=body.text,
        operation_id=body.operation_id,
        namespace=body.namespace,
        sources=body.sources,
        importance=body.importance,
    )
    return _blob_out(blob)


@router.post("/search", response_model=list[MemoryHitOut])
async def search(
    body: MemorySearchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[MemoryHitOut]:
    hits = await svc.retrieve(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_id=body.project_id,
        query=body.query,
        operation_id=body.operation_id,
        limit=body.limit,
        grader_min_rrf=body.grader_min_rrf,
    )
    return [MemoryHitOut(blob=_blob_out(h.blob), rrf=h.rrf) for h in hits]


@router.get("/blobs/{blob_id}", response_model=MemoryBlobOut)
async def get_blob(
    blob_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> MemoryBlobOut:
    return _blob_out(await svc.get_blob(ctx.session, org_id=ctx.org_id, blob_id=blob_id))


@router.post("/erase", response_model=ErasedOut)
async def erase(
    body: MemoryEraseIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> ErasedOut:
    deleted = await svc.gdpr_erase(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        source_kind=body.source_kind,
        source_id=body.source_id,
    )
    return ErasedOut(deleted=deleted)


@router.post("/consolidate", response_model=MemoryBlobOut)
async def consolidate(
    body: MemoryConsolidateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> MemoryBlobOut:
    blob = await svc.consolidate(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_id=body.project_id,
        blob_ids=body.blob_ids,
        operation_id=body.operation_id,
    )
    return _blob_out(blob)


@router.post("/recompute-tier", response_model=TierCountsOut)
async def recompute_tier(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> TierCountsOut:
    counts = await svc.recompute_tier(ctx.session, org_id=ctx.org_id)
    return TierCountsOut(hot=counts["hot"], warm=counts["warm"], cold=counts["cold"])
