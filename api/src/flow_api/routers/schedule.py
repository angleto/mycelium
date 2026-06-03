"""Schedule router: deterministic recompute, read, and per-task
scheduler-field write-back (FR-4, docs/adr/0004). Thin adapter; the
derived schedule is not under user optimistic concurrency (latest
recompute wins)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    RecomputeIn,
    RecomputeOut,
    ScheduleOut,
    TaskScheduleIn,
    VersionOut,
)
from flow_core.errors import NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.schedule import Schedule
from flow_core.services import scheduler as svc
from flow_core.services import tasks as tasks_svc

router = APIRouter(tags=["schedule"])


def _out(s: Schedule) -> ScheduleOut:
    return ScheduleOut(
        task_id=s.task_id,
        es=s.es,
        ef=s.ef,
        ls=s.ls,
        lf=s.lf,
        slack_minutes=s.slack_minutes,
        on_logical_critical_path=s.on_logical_critical_path,
        on_critical_chain=s.on_critical_chain,
        projected_cost=s.projected_cost,
        scheduled_start=s.scheduled_start,
        scheduled_end=s.scheduled_end,
        assigned_executor_id=s.assigned_executor_id,
        unassignable=s.unassignable,
        unassignable_reason=s.unassignable_reason,
        computed_at=s.computed_at,
        input_fingerprint=s.input_fingerprint,
    )


@router.post("/schedule/recompute", response_model=RecomputeOut)
async def recompute(
    body: RecomputeIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> RecomputeOut:
    summary = await svc.recompute(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_tag_id=body.project_tag_id,
        as_of=body.as_of,
        policy=body.policy,
    )
    return RecomputeOut(
        count=summary.count,
        makespan_minutes=summary.makespan_minutes,
        projected_credit_cost=summary.projected_credit_cost,
        policy=summary.policy,
        unassignable_count=summary.unassignable_count,
    )


@router.get("/schedule", response_model=list[ScheduleOut])
async def list_schedule(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    project_tag_id: uuid.UUID | None = None,
) -> list[ScheduleOut]:
    rows = await svc.list_schedule(ctx.session, org_id=ctx.org_id, project_tag_id=project_tag_id)
    return [_out(s) for s in rows]


@router.get("/schedule/{task_id}", response_model=ScheduleOut)
async def get_schedule(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> ScheduleOut:
    row = await svc.get_schedule(ctx.session, org_id=ctx.org_id, task_id=task_id)
    if row is None:
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    return _out(row)


@router.patch("/tasks/{task_id}/schedule", response_model=VersionOut)
async def set_task_schedule(
    task_id: uuid.UUID,
    body: TaskScheduleIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    values = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    version = await tasks_svc.set_schedule_fields(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
        values=values,
    )
    return VersionOut(id=task_id, version=version)
