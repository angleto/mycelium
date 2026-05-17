"""Advisory router: deterministic planning queries (docs/adr/0001,
0013, FR-13). The decision core is in the service layer; this is a
thin adapter. Operates on the user's tasks within the org."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    BudgetPickOut,
    BudgetPlanOut,
    ErrandItemOut,
    ErrandsIn,
    FeasibleTaskOut,
    WhatNowIn,
)
from flow_core.services import advisory as svc

router = APIRouter(prefix="/advisory", tags=["advisory"])


@router.post("/what-now", response_model=list[FeasibleTaskOut])
async def what_now(
    body: WhatNowIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[FeasibleTaskOut]:
    rows = await svc.what_can_i_do_now(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        window_start=body.window_start,
        duration_minutes=body.duration_minutes,
        location=body.location,
        context_tags=body.context_tags,
    )
    return [
        FeasibleTaskOut(
            task_id=r.task_id,
            title=r.title,
            necessity=r.necessity,
            priority=r.priority,
            due_date=r.due_date,
            remaining_minutes=r.remaining_minutes,
        )
        for r in rows
    ]


@router.post("/errands", response_model=list[ErrandItemOut])
async def errands(
    body: ErrandsIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[ErrandItemOut]:
    rows = await svc.errands(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        location=body.location,
        context=body.context,
    )
    return [
        ErrandItemOut(
            task_id=r.task_id,
            title=r.title,
            location=r.location,
            necessity=r.necessity,
            priority=r.priority,
        )
        for r in rows
    ]


@router.get("/budget/{budget_id}/plan", response_model=BudgetPlanOut)
async def budget_plan(
    budget_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> BudgetPlanOut:
    plan = await svc.prioritize_within_budget(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        budget_id=budget_id,
    )
    return BudgetPlanOut(
        budget_id=plan.budget_id,
        amount=plan.amount,
        currency=plan.currency,
        allocated=plan.allocated,
        residual=plan.residual,
        selected=[
            BudgetPickOut(
                task_id=p.task_id,
                title=p.title,
                cost=p.cost,
                necessity=p.necessity,
                priority=p.priority,
                value=p.value,
            )
            for p in plan.selected
        ],
        excluded=plan.excluded,
    )
