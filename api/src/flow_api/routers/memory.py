"""Memory router: write, hybrid search (retrieval-as-tool), get, GDPR
erase, consolidate, tier recompute, tag curation. Thin adapter
(docs/adr/0001, 0003, 0005, 0007, 0016, FR-8). The (org, project)
predicate is enforced in the service; tags are an orthogonal facet
inside that boundary; the embedder is injected (fake in tests)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    ErasedOut,
    MemoryBlobOut,
    MemoryConsolidateIn,
    MemoryEraseIn,
    MemoryHitOut,
    MemorySearchIn,
    MemoryStatusOut,
    MemoryWriteIn,
    TagBrief,
    TagRefIn,
    TierCountsOut,
)
from flow_core.embedder import embedder_available
from flow_core.models.memory_blob import MemoryBlob
from flow_core.models.tag import Tag
from flow_core.services import memory as svc

router = APIRouter(prefix="/memory", tags=["memory"])


def _blob_out(b: MemoryBlob, tags: list[Tag] | None = None) -> MemoryBlobOut:
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
        tags=[TagBrief(id=g.id, kind=g.kind, name=g.name, color=g.color) for g in (tags or [])],
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
        tag_ids=body.tag_ids,
        channel_tag_id=body.channel_tag_id,
    )
    tagmap = await svc.tags_by_blob(ctx.session, blob_ids=[blob.id])
    return _blob_out(blob, tagmap.get(blob.id))


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
        tag_ids=body.tag_ids,
        channel_tag_id=body.channel_tag_id,
    )
    tagmap = await svc.tags_by_blob(ctx.session, blob_ids=[h.blob.id for h in hits])
    return [MemoryHitOut(blob=_blob_out(h.blob, tagmap.get(h.blob.id)), rrf=h.rrf) for h in hits]


@router.get("/status", response_model=MemoryStatusOut)
async def status_(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> MemoryStatusOut:
    """Whether semantic retrieval is available (the optional embedding
    model is installed) or memory is running keyword-only. Member-level
    via tenant_ctx; lets the SPA show "semantic vs keyword-only"."""
    return MemoryStatusOut(semantic=embedder_available())


@router.get("/blobs/{blob_id}", response_model=MemoryBlobOut)
async def get_blob(
    blob_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> MemoryBlobOut:
    blob = await svc.get_blob(ctx.session, org_id=ctx.org_id, blob_id=blob_id)
    tagmap = await svc.tags_by_blob(ctx.session, blob_ids=[blob.id])
    return _blob_out(blob, tagmap.get(blob.id))


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
    tagmap = await svc.tags_by_blob(ctx.session, blob_ids=[blob.id])
    return _blob_out(blob, tagmap.get(blob.id))


@router.post("/blobs/{blob_id}/tags", status_code=status.HTTP_204_NO_CONTENT)
async def attach_blob_tag(
    blob_id: uuid.UUID,
    body: TagRefIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.attach_blob_tag(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        blob_id=blob_id,
        tag_id=body.tag_id,
    )


@router.delete("/blobs/{blob_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_blob_tag(
    blob_id: uuid.UUID,
    tag_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.detach_blob_tag(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        blob_id=blob_id,
        tag_id=tag_id,
    )


@router.post("/recompute-tier", response_model=TierCountsOut)
async def recompute_tier(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> TierCountsOut:
    counts = await svc.recompute_tier(ctx.session, org_id=ctx.org_id)
    return TierCountsOut(hot=counts["hot"], warm=counts["warm"], cold=counts["cold"])
