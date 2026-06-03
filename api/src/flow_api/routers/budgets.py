"""Budgets router: envelope CRUD + consumption. Thin adapter over the
service layer (docs/adr/0001, 0014, FR-14)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    BudgetCreateIn,
    BudgetOut,
    BudgetPatchIn,
    ConsumptionOut,
    VersionOut,
)
from flow_core.models.budget import Budget
from flow_core.services import budgets as svc

router = APIRouter(tags=["budgets"])


def _out(b: Budget) -> BudgetOut:
    return BudgetOut(
        id=b.id,
        name=b.name,
        category=b.category,
        period_kind=b.period_kind,
        period_start=b.period_start,
        period_end=b.period_end,
        amount=b.amount,
        currency=b.currency,
        version=b.version,
    )


@router.post("/budgets", response_model=BudgetOut)
async def create_budget(
    body: BudgetCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> BudgetOut:
    b = await svc.create_budget(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        name=body.name,
        category=body.category,
        period_kind=body.period_kind,
        period_start=body.period_start,
        period_end=body.period_end,
        amount=body.amount,
        currency=body.currency,
    )
    return _out(b)


@router.get("/budgets", response_model=list[BudgetOut])
async def list_budgets(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[BudgetOut]:
    return [_out(b) for b in await svc.list_budgets(ctx.session, org_id=ctx.org_id)]


@router.get("/budgets/{budget_id}", response_model=BudgetOut)
async def get_budget(
    budget_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> BudgetOut:
    return _out(await svc.get_budget(ctx.session, org_id=ctx.org_id, budget_id=budget_id))


@router.patch("/budgets/{budget_id}", response_model=VersionOut)
async def update_budget(
    budget_id: uuid.UUID,
    body: BudgetPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    values = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    version = await svc.update_budget(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        budget_id=budget_id,
        expected_version=body.expected_version,
        values=values,
    )
    return VersionOut(id=budget_id, version=version)


@router.delete("/budgets/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(
    budget_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.delete_budget(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        budget_id=budget_id,
    )


@router.get("/budgets/{budget_id}/consumption", response_model=ConsumptionOut)
async def consumption(
    budget_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> ConsumptionOut:
    c = await svc.consumption(ctx.session, org_id=ctx.org_id, budget_id=budget_id)
    return ConsumptionOut(
        budget_id=c.budget_id,
        amount=c.amount,
        currency=c.currency,
        consumed=c.consumed,
        residual=c.residual,
        task_count=c.task_count,
    )
