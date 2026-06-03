"""Executor registry router (docs/adr/0025, P2). Thin adapter over the
service layer (docs/adr/0001). Reads are member-level (the schedule
plan must show its assignments); mutations are owner-gated inside the
service (the RBAC choke point + effective-role sudo), mirroring the
rate-card / issuer-profile precedent for workspace config CRUD."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    ExecutorCreateIn,
    ExecutorOut,
    ExecutorPatchIn,
    VersionOut,
)
from flow_core.models.executor import Executor
from flow_core.services import executors as svc

router = APIRouter(prefix="/executors", tags=["executors"])


def _out(e: Executor) -> ExecutorOut:
    return ExecutorOut(
        id=e.id,
        kind=e.kind,
        name=e.name,
        user_id=e.user_id,
        context_switch_cost_minutes=e.context_switch_cost_minutes,
        provider=e.provider,
        model_id=e.model_id,
        max_parallel=e.max_parallel,
        credit_budget=e.credit_budget,
        credit_rate_per_hour=e.credit_rate_per_hour,
        enabled=e.enabled,
        capability_tags=list(e.capability_tags or []),
        version=e.version,
    )


@router.get("", response_model=list[ExecutorOut])
async def list_executors(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[ExecutorOut]:
    rows = await svc.list_executors(ctx.session, org_id=ctx.org_id)
    return [_out(e) for e in rows]


@router.post("", response_model=ExecutorOut)
async def create_executor(
    body: ExecutorCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> ExecutorOut:
    row = await svc.create_executor(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        kind=body.kind,
        name=body.name,
        user_id=body.user_id,
        context_switch_cost_minutes=body.context_switch_cost_minutes,
        provider=body.provider,
        model_id=body.model_id,
        max_parallel=body.max_parallel,
        credit_budget=body.credit_budget,
        credit_rate_per_hour=body.credit_rate_per_hour,
        enabled=body.enabled,
        capability_tags=body.capability_tags,
    )
    return _out(row)


@router.patch("/{executor_id}", response_model=VersionOut)
async def update_executor(
    executor_id: uuid.UUID,
    body: ExecutorPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    values = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    version = await svc.update_executor(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        executor_id=executor_id,
        expected_version=body.expected_version,
        values=values,
    )
    return VersionOut(id=executor_id, version=version)


@router.delete("/{executor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_executor(
    executor_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.delete_executor(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        executor_id=executor_id,
    )
