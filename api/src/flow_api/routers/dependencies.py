"""Dependencies + graph router. Thin adapter over the service layer
(docs/adr/0001, 0004, FR-3)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    DependencyCreateIn,
    DependencyOut,
    GraphEdge,
    GraphNode,
    GraphOut,
)
from flow_core.services import dependencies as svc

router = APIRouter(tags=["dependencies"])


@router.post("/dependencies", response_model=DependencyOut)
async def add_dependency(
    body: DependencyCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> DependencyOut:
    d = await svc.add_dependency(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        predecessor_id=body.predecessor_id,
        successor_id=body.successor_id,
        type=body.type,
        lag_working_minutes=body.lag_working_minutes,
    )
    return DependencyOut(
        id=d.id,
        predecessor_id=d.predecessor_id,
        successor_id=d.successor_id,
        type=d.type,
        lag_working_minutes=d.lag_working_minutes,
        version=d.version,
    )


@router.delete(
    "/dependencies/{dependency_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_dependency(
    dependency_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.remove_dependency(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        dependency_id=dependency_id,
    )


@router.get("/dependencies", response_model=list[DependencyOut])
async def list_dependencies(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    task_id: uuid.UUID | None = None,
) -> list[DependencyOut]:
    rows = await svc.list_dependencies(ctx.session, org_id=ctx.org_id, task_id=task_id)
    return [
        DependencyOut(
            id=d.id,
            predecessor_id=d.predecessor_id,
            successor_id=d.successor_id,
            type=d.type,
            lag_working_minutes=d.lag_working_minutes,
            version=d.version,
        )
        for d in rows
    ]


@router.get("/graph", response_model=GraphOut)
async def graph(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    project_tag_id: uuid.UUID | None = None,
) -> GraphOut:
    g = await svc.graph(ctx.session, org_id=ctx.org_id, project_tag_id=project_tag_id)
    return GraphOut(
        nodes=[GraphNode(**n) for n in g["nodes"]],
        edges=[GraphEdge(**e) for e in g["edges"]],
    )
