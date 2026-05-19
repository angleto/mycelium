"""Agent execution runtime router (docs/adr/0025, P3). Thin adapter
over the service layer (docs/adr/0001).

``POST /tasks/{task_id}/run`` spawns and drives ONE agent run for an
already-dispatched ``llm_agent`` task and returns the FINAL run (P3 is
on-demand, not an autonomous loop). Reads are member-level; start /
cancel are owner-gated INSIDE the service (the RBAC choke point +
effective-role sudo), because running an agent spends credits -- same
gate model as billing grants. The router never embeds business logic;
the governance (tool allowlist, step/budget caps, kill switch) lives in
``flow_core.services.agent_runtime``."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import AgentRunOut
from flow_core.models.agent_run import AgentRun
from flow_core.services import agent_runtime as svc

router = APIRouter(tags=["agent-runs"])


def _out(r: AgentRun) -> AgentRunOut:
    return AgentRunOut(
        id=r.id,
        task_id=r.task_id,
        executor_id=r.executor_id,
        status=r.status,
        steps=r.steps,
        credits_spent=r.credits_spent,
        started_at=r.started_at,
        ended_at=r.ended_at,
        error=r.error,
        artifact_note_id=r.artifact_note_id,
        cancel_requested=r.cancel_requested,
        blocked_reason=r.blocked_reason,
        version=r.version,
    )


@router.post("/tasks/{task_id}/run", response_model=AgentRunOut)
async def start_run(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> AgentRunOut:
    """Owner: run the agent on this dispatched ``llm_agent`` task,
    end-to-end. Returns the terminal run (succeeded|failed|cancelled|
    blocked). Owner-gated in the service (effective-role sudo
    enforced)."""
    run = await svc.start_run(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
    )
    return _out(run)


@router.get("/agent-runs", response_model=list[AgentRunOut])
async def list_runs(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    task_id: uuid.UUID | None = None,
) -> list[AgentRunOut]:
    """List agent runs (member-level), newest first, optionally filtered
    to one task."""
    rows = await svc.list_runs(ctx.session, org_id=ctx.org_id, task_id=task_id)
    return [_out(r) for r in rows]


@router.get("/agent-runs/{run_id}", response_model=AgentRunOut)
async def get_run(
    run_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> AgentRunOut:
    """Read one agent run (member-level, RLS-scoped)."""
    run = await svc.get_run(ctx.session, org_id=ctx.org_id, run_id=run_id)
    return _out(run)


@router.post("/agent-runs/{run_id}/cancel", response_model=AgentRunOut)
async def cancel_run(
    run_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> AgentRunOut:
    """Owner: request cancellation (cooperative kill switch the loop
    observes). Idempotent; a terminal run -> 400. Owner-gated in the
    service."""
    run = await svc.cancel_run(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        run_id=run_id,
    )
    return _out(run)
