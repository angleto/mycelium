"""Symmetric "related task" router. Pure navigation aid: no scheduling
semantics (see ``routers/dependencies.py`` for those). Pairs are
canonicalised in the service so callers can pass the two ids in any
order."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import TaskRelationCreateIn, TaskRelationOut
from flow_core.models.task_relation import TaskRelation
from flow_core.services import task_relations as svc

router = APIRouter(tags=["task-relations"])


def _out(r: TaskRelation) -> TaskRelationOut:
    return TaskRelationOut(
        id=r.id,
        task_a_id=r.task_a_id,
        task_b_id=r.task_b_id,
        version=r.version,
    )


@router.post("/task-relations", response_model=TaskRelationOut)
async def add_task_relation(
    body: TaskRelationCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> TaskRelationOut:
    rel = await svc.add_relation(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=body.task_id,
        other_id=body.other_id,
    )
    return _out(rel)


@router.delete(
    "/task-relations/{relation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_task_relation(
    relation_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.remove_relation(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        relation_id=relation_id,
    )


@router.get("/task-relations", response_model=list[TaskRelationOut])
async def list_task_relations(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    task_id: uuid.UUID | None = None,
) -> list[TaskRelationOut]:
    rows = await svc.list_relations(ctx.session, org_id=ctx.org_id, task_id=task_id)
    return [_out(r) for r in rows]
