"""Closed-loop dispatch + approval-gate router (docs/adr/0025, P5).
Thin adapter over the service layer (docs/adr/0001).

Reads (the dispatch queue) are member-level: the team can see what the
loop proposed. Approve / deny / manual tick are owner-gated INSIDE the
service (the RBAC choke point + effective-role sudo), because a tick can
spend credits via the P3 metered path -- same gate model as the P3 run
start and the billing grant. The router never embeds business logic;
the closed loop, the at-most-one-active-request invariant and the
human-in-the-loop governance live in ``mycelium_core.services.dispatch_loop``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import (
    DispatchDecisionIn,
    DispatchRequestOut,
    DispatchTickIn,
    DispatchTickOut,
)
from mycelium_core.models.dispatch_request import DispatchRequest
from mycelium_core.models.executor import Executor
from mycelium_core.models.task import Task
from mycelium_core.services import dispatch_loop as svc

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


async def _out(ctx: TenantCtx, r: DispatchRequest) -> DispatchRequestOut:
    """Denormalize the task title + executor name for the queue UI
    (RLS-scoped lookups; a missing row just yields empty/None)."""
    title = (
        await ctx.session.execute(select(Task.title).where(Task.id == r.task_id))
    ).scalar_one_or_none() or ""
    exec_name: str | None = None
    if r.executor_id is not None:
        exec_name = (
            await ctx.session.execute(select(Executor.name).where(Executor.id == r.executor_id))
        ).scalar_one_or_none()
    return DispatchRequestOut(
        id=r.id,
        task_id=r.task_id,
        task_title=title,
        executor_id=r.executor_id,
        executor_name=exec_name,
        status=r.status,
        projected_credit_cost=r.projected_credit_cost,
        agent_run_id=r.agent_run_id,
        requested_at=r.requested_at,
        decided_at=r.decided_at,
        decided_by=r.decided_by,
        reason=r.reason,
        version=r.version,
    )


@router.get("/requests", response_model=list[DispatchRequestOut])
async def list_requests(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[DispatchRequestOut]:
    """Member: the dispatch queue (RLS-scoped), newest first. Each row
    carries the task title, the assigned executor name, the projected
    credit cost and the status for the approval UI."""
    rows = await svc.list_requests(ctx.session, org_id=ctx.org_id)
    return [await _out(ctx, r) for r in rows]


@router.post("/requests/{request_id}/approve", response_model=DispatchRequestOut)
async def approve_request(
    request_id: uuid.UUID,
    body: DispatchDecisionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> DispatchRequestOut:
    """Owner: approve a pending request, then immediately attempt the
    dispatch inline (approve-then-inline-dispatch -- the caller can
    assert the run started in this call; the worker tick dispatches any
    leftover ``approved`` row identically). Owner-gated in the service
    (a dispatch spends credits; effective-role sudo enforced).
    Optimistic concurrency on ``expected_version``."""
    req = await svc.approve_request(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        request_id=request_id,
        expected_version=body.expected_version,
    )
    return await _out(ctx, req)


@router.post("/requests/{request_id}/deny", response_model=DispatchRequestOut)
async def deny_request(
    request_id: uuid.UUID,
    body: DispatchDecisionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> DispatchRequestOut:
    """Owner: deny an active request (never starts a run), with an
    optional short reason. Owner-gated in the service; optimistic
    concurrency."""
    req = await svc.deny_request(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        request_id=request_id,
        expected_version=body.expected_version,
        reason=body.reason,
    )
    return await _out(ctx, req)


@router.post("/tick", response_model=DispatchTickOut)
async def tick(
    body: DispatchTickIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> DispatchTickOut:
    """Owner: run one closed-loop tick now (recompute -> admit -> gate
    -> dispatch). The worker calls the same service on a timer; this
    endpoint makes it testable and gives the UI a "run now". Owner-gated
    in the service (a tick can spend credits via P3)."""
    res = await svc.tick(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        policy=body.policy,
    )
    return DispatchTickOut(
        policy=res.policy,
        enabled=res.enabled,
        created=res.created,
        approved=res.approved,
        dispatched=res.dispatched,
        skipped=res.skipped,
        failed=res.failed,
        projected_makespan_minutes=res.projected_makespan_minutes,
        projected_credit_cost=res.projected_credit_cost,
    )
