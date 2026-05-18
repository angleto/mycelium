"""Time tracking router: live timer, manual entries, reports, CSV
export. Thin adapter over the service layer (docs/adr/0001, FR-5).

PDF export is a thin presentation-only follow-up (CSV already
satisfies the data-export requirement); see roadmap F4."""

from __future__ import annotations

import csv
import datetime
import io
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from starlette.responses import Response

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    ReportRowOut,
    TimeEntryOut,
    TimeEntryPatchIn,
    TimeManualIn,
    TimeStartIn,
    TimeStopIn,
    VersionOut,
)
from flow_core.models.task import ExecKind
from flow_core.models.time_entry import TimeEntry
from flow_core.services import time_tracking as svc
from flow_core.services.time_tracking import ReportGroup

router = APIRouter(tags=["time"])


def _out(e: TimeEntry) -> TimeEntryOut:
    return TimeEntryOut(
        id=e.id,
        task_id=e.task_id,
        user_id=e.user_id,
        started_at=e.started_at,
        ended_at=e.ended_at,
        duration_seconds=e.duration_seconds,
        source=e.source,
        executor_kind=e.executor_kind,
        billable=e.billable,
        rate_snapshot=e.rate_snapshot,
        currency=e.currency,
        note=e.note,
        version=e.version,
    )


@router.post("/time/start", response_model=TimeEntryOut)
async def start_timer(
    body: TimeStartIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> TimeEntryOut:
    e = await svc.start_timer(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=body.task_id,
        billable=body.billable,
        note=body.note,
    )
    return _out(e)


@router.post("/time/stop", response_model=TimeEntryOut)
async def stop_timer(
    body: TimeStopIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> TimeEntryOut:
    e = await svc.stop_timer(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        note=body.note,
    )
    return _out(e)


@router.get("/time/running", response_model=TimeEntryOut | None)
async def running(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> TimeEntryOut | None:
    e = await svc.running_entry(ctx.session, org_id=ctx.org_id, user_id=ctx.user_id)
    return _out(e) if e is not None else None


@router.post("/time/entries", response_model=TimeEntryOut)
async def add_manual_entry(
    body: TimeManualIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> TimeEntryOut:
    e = await svc.add_manual_entry(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=body.task_id,
        started_at=body.started_at,
        ended_at=body.ended_at,
        duration_seconds=body.duration_seconds,
        billable=body.billable,
        note=body.note,
    )
    return _out(e)


@router.get("/time/entries", response_model=list[TimeEntryOut])
async def list_entries(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    task_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
    billable: bool | None = None,
) -> list[TimeEntryOut]:
    rows = await svc.list_entries(
        ctx.session,
        org_id=ctx.org_id,
        task_id=task_id,
        user_id=user_id,
        start_from=start_from,
        start_to=start_to,
        billable=billable,
    )
    return [_out(e) for e in rows]


@router.get("/time/entries/{entry_id}", response_model=TimeEntryOut)
async def get_entry(
    entry_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> TimeEntryOut:
    return _out(await svc.get_entry(ctx.session, org_id=ctx.org_id, entry_id=entry_id))


@router.patch("/time/entries/{entry_id}", response_model=VersionOut)
async def update_entry(
    entry_id: uuid.UUID,
    body: TimeEntryPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> VersionOut:
    values = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    version = await svc.update_entry(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        entry_id=entry_id,
        expected_version=body.expected_version,
        values=values,
    )
    return VersionOut(id=entry_id, version=version)


@router.delete("/time/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    entry_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.delete_entry(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        entry_id=entry_id,
    )


async def _report(
    ctx: TenantCtx,
    group_by: ReportGroup,
    start_from: datetime.datetime | None,
    start_to: datetime.datetime | None,
    billable: bool | None,
    executor_kind: ExecKind | None,
) -> list[svc.ReportRow]:
    return await svc.report(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        group_by=group_by,
        start_from=start_from,
        start_to=start_to,
        billable=billable,
        executor_kind=executor_kind,
    )


@router.get("/time/report", response_model=list[ReportRowOut])
async def report(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    group_by: ReportGroup = ReportGroup.project,
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
    billable: bool | None = None,
    executor_kind: ExecKind | None = None,
) -> list[ReportRowOut]:
    rows = await _report(
        ctx, group_by, start_from, start_to, billable, executor_kind
    )
    return [
        ReportRowOut(
            key=r.key,
            label=r.label,
            seconds=r.seconds,
            billable_seconds=r.billable_seconds,
            amount=r.amount,
            currency=r.currency,
        )
        for r in rows
    ]


@router.get("/time/report.csv")
async def report_csv(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    group_by: ReportGroup = ReportGroup.project,
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
    billable: bool | None = None,
    executor_kind: ExecKind | None = None,
) -> Response:
    rows = await _report(
        ctx, group_by, start_from, start_to, billable, executor_kind
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["key", "label", "seconds", "billable_seconds", "amount", "currency"])
    for r in rows:
        w.writerow(
            [
                r.key or "",
                r.label or "",
                r.seconds,
                r.billable_seconds,
                f"{r.amount}",
                r.currency,
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="time-report.csv"'},
    )
