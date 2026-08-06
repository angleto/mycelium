"""Time tracking router: live timer, manual entries, reports, CSV
export. Thin adapter over the service layer (docs/adr/0001, FR-5).

PDF export is a thin presentation-only follow-up (CSV already
satisfies the data-export requirement); see roadmap F4."""

from __future__ import annotations

import csv
import datetime
import io
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from starlette.responses import Response

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import (
    DailyReportRowOut,
    ReportRowOut,
    TaskTimeReportOut,
    TimeEntryOut,
    TimeEntryPatchIn,
    TimeManualIn,
    TimePauseIn,
    TimeResumeIn,
    TimeStartIn,
    TimeStopIn,
    VersionOut,
)
from mycelium_core.models.task import ExecKind
from mycelium_core.models.time_entry import TimeEntry
from mycelium_core.services import time_tracking as svc
from mycelium_core.services.time_tracking import ReportGroup, TaskContext

router = APIRouter(tags=["time"])

_EMPTY_CTX = TaskContext()


def _out(
    e: TimeEntry,
    ctx: TaskContext = _EMPTY_CTX,
    note_title: str | None = None,
) -> TimeEntryOut:
    return TimeEntryOut(
        id=e.id,
        task_id=e.task_id,
        user_id=e.user_id,
        started_at=e.started_at,
        ended_at=e.ended_at,
        duration_seconds=e.duration_seconds,
        accumulated_seconds=e.accumulated_seconds,
        resumed_at=e.resumed_at,
        source=e.source,
        executor_kind=e.executor_kind,
        billable=e.billable,
        parallel=e.parallel,
        rate_snapshot=e.rate_snapshot,
        currency=e.currency,
        memo=e.memo,
        note_id=e.note_id,
        note_title=note_title,
        version=e.version,
        task_title=ctx.task_title,
        client_tag_id=ctx.client_tag_id,
        client_name=ctx.client_name,
        project_tag_id=ctx.project_tag_id,
        project_name=ctx.project_name,
        client_timezone=ctx.client_timezone,
    )


async def _out_one(ctx: TenantCtx, e: TimeEntry) -> TimeEntryOut:
    titles = await svc.resolve_note_titles(ctx.session, [e.note_id])
    note_title = titles.get(e.note_id) if e.note_id is not None else None
    return _out(e, await svc.context_for_entry(ctx.session, e), note_title)


async def _out_many(ctx: TenantCtx, rows: list[TimeEntry]) -> list[TimeEntryOut]:
    ctxs = await svc.resolve_task_contexts(ctx.session, [e.task_id for e in rows])
    titles = await svc.resolve_note_titles(ctx.session, [e.note_id for e in rows])
    return [
        _out(
            e,
            ctxs.get(e.task_id, _EMPTY_CTX),
            titles.get(e.note_id) if e.note_id is not None else None,
        )
        for e in rows
    ]


@router.post("/time/start", response_model=TimeEntryOut)
async def start_timer(
    body: TimeStartIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TimeEntryOut:
    e = await svc.start_timer(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=body.task_id,
        billable=body.billable,
        memo=body.memo,
        note_id=body.note_id,
        parallel=body.parallel,
    )
    return await _out_one(ctx, e)


@router.post("/time/stop", response_model=TimeEntryOut)
async def stop_timer(
    body: TimeStopIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TimeEntryOut:
    e = await svc.stop_timer(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=body.task_id,
        memo=body.memo,
    )
    return await _out_one(ctx, e)


@router.post("/time/pause", response_model=TimeEntryOut)
async def pause_timer(
    body: TimePauseIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TimeEntryOut:
    e = await svc.pause_timer(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=body.task_id,
    )
    return await _out_one(ctx, e)


@router.post("/time/resume", response_model=TimeEntryOut)
async def resume_timer(
    body: TimeResumeIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TimeEntryOut:
    e = await svc.resume_timer(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=body.task_id,
    )
    return await _out_one(ctx, e)


@router.get("/time/running", response_model=list[TimeEntryOut])
async def running(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[TimeEntryOut]:
    rows = await svc.running_entries(ctx.session, org_id=ctx.org_id, user_id=ctx.user_id)
    return await _out_many(ctx, rows)


@router.post("/time/entries", response_model=TimeEntryOut)
async def add_manual_entry(
    body: TimeManualIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
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
        memo=body.memo,
        note_id=body.note_id,
    )
    return await _out_one(ctx, e)


@router.get("/time/entries", response_model=list[TimeEntryOut])
async def list_entries(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    task_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
    billable: bool | None = None,
    client_tag_id: uuid.UUID | None = None,
    project_tag_id: uuid.UUID | None = None,
    # Range-validated but deliberately still unbounded by DEFAULT: the
    # task drill-down in TimeRoute relies on getting every entry that
    # matches its filters, so a default page size would silently truncate
    # it. Bounding it needs that panel to page first.
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TimeEntryOut]:
    rows = await svc.list_entries(
        ctx.session,
        org_id=ctx.org_id,
        task_id=task_id,
        user_id=user_id,
        start_from=start_from,
        start_to=start_to,
        billable=billable,
        client_tag_id=client_tag_id,
        project_tag_id=project_tag_id,
        limit=limit,
        offset=offset,
    )
    return await _out_many(ctx, rows)


@router.get("/time/entries/{entry_id}", response_model=TimeEntryOut)
async def get_entry(
    entry_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TimeEntryOut:
    e = await svc.get_entry(ctx.session, org_id=ctx.org_id, entry_id=entry_id)
    return await _out_one(ctx, e)


@router.patch("/time/entries/{entry_id}", response_model=VersionOut)
async def update_entry(
    entry_id: uuid.UUID,
    body: TimeEntryPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
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
    client_tag_id: uuid.UUID | None,
    project_tag_id: uuid.UUID | None,
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
        client_tag_id=client_tag_id,
        project_tag_id=project_tag_id,
    )


@router.get("/time/report", response_model=list[ReportRowOut])
async def report(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    group_by: ReportGroup = ReportGroup.project,
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
    billable: bool | None = None,
    executor_kind: ExecKind | None = None,
    client_tag_id: uuid.UUID | None = None,
    project_tag_id: uuid.UUID | None = None,
) -> list[ReportRowOut]:
    rows = await _report(
        ctx,
        group_by,
        start_from,
        start_to,
        billable,
        executor_kind,
        client_tag_id,
        project_tag_id,
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


@router.get("/time/report/daily", response_model=list[DailyReportRowOut])
async def report_daily(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    group_by: ReportGroup = ReportGroup.project,
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
    billable: bool | None = None,
    executor_kind: ExecKind | None = None,
    client_tag_id: uuid.UUID | None = None,
    project_tag_id: uuid.UUID | None = None,
    tz: str | None = None,
) -> list[DailyReportRowOut]:
    """``GET /time/report`` split by calendar day: identical buckets and
    identical filter knobs, so the histogram and the donut drawn from
    them cannot disagree. ``tz`` is an IANA name (default UTC) deciding
    where a day starts; an unknown one is rejected rather than ignored.
    Only days with tracked time are returned — the SPA zero-fills the
    selected range, which keeps the payload proportional to the data."""
    rows = await svc.daily_report(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        group_by=group_by,
        start_from=start_from,
        start_to=start_to,
        billable=billable,
        executor_kind=executor_kind,
        client_tag_id=client_tag_id,
        project_tag_id=project_tag_id,
        tz=tz,
    )
    return [
        DailyReportRowOut(
            day=r.day,
            key=r.key,
            label=r.label,
            seconds=r.seconds,
            billable_seconds=r.billable_seconds,
            amount=r.amount,
            currency=r.currency,
        )
        for r in rows
    ]


@router.get("/time/report/daily.csv")
async def report_daily_csv(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    group_by: ReportGroup = ReportGroup.project,
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
    billable: bool | None = None,
    executor_kind: ExecKind | None = None,
    client_tag_id: uuid.UUID | None = None,
    project_tag_id: uuid.UUID | None = None,
    tz: str | None = None,
) -> Response:
    """The per-day report as CSV, one row per (day, bucket). This is the
    billing artifact: ``/time/report.csv`` collapses a period into one row
    per client/project, which is not enough to invoice against a client who
    wants the days itemised. Same buckets, same filters and the same ``tz``
    day boundary as the on-screen histogram, so the file reconciles with
    what the operator saw before exporting it."""
    rows = await svc.daily_report(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        group_by=group_by,
        start_from=start_from,
        start_to=start_to,
        billable=billable,
        executor_kind=executor_kind,
        client_tag_id=client_tag_id,
        project_tag_id=project_tag_id,
        tz=tz,
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["day", "key", "label", "seconds", "hours", "billable_seconds", "amount", "currency"]
    )
    for r in rows:
        w.writerow(
            [
                r.day.isoformat(),
                r.key or "",
                r.label or "",
                r.seconds,
                # Decimal, not float: an invoice line is money arithmetic and
                # 3600ths of an hour must not pick up a binary rounding tail.
                f"{(Decimal(r.seconds) / Decimal(3600)).quantize(Decimal('0.01'))}",
                r.billable_seconds,
                f"{r.amount}",
                r.currency,
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="time-report-daily.csv"'},
    )


@router.get("/time/report/by-task", response_model=list[TaskTimeReportOut])
async def report_by_task(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
    billable: bool | None = None,
    executor_kind: ExecKind | None = None,
    client_tag_id: uuid.UUID | None = None,
    project_tag_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> list[TaskTimeReportOut]:
    """Per-task aggregate (total/billable/count), each row carrying
    resolved project/client/timezone so the SPA's drill-down (entries of
    a task) is just ``GET /time/entries`` filtered client-side. Distinct
    path from the configurable ``/time/report`` (which keeps its
    ReportRowOut contract), but the same filter knobs: this table sits
    under the report charts and must answer the same question they do.

    Org-wide unless ``user_id`` narrows it, mirroring ``GET
    /time/entries``. It was implicitly the caller's own entries, which
    made it the one panel on the page disagreeing with everything around
    it in a multi-member workspace."""
    rows = await svc.task_report(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        start_from=start_from,
        start_to=start_to,
        billable=billable,
        executor_kind=executor_kind,
        client_tag_id=client_tag_id,
        project_tag_id=project_tag_id,
        user_id=user_id,
    )
    return [
        TaskTimeReportOut(
            task_id=r.task_id,
            task_title=r.task_title,
            client_tag_id=r.client_tag_id,
            client_name=r.client_name,
            project_tag_id=r.project_tag_id,
            project_name=r.project_name,
            client_timezone=r.client_timezone,
            total_seconds=r.total_seconds,
            billable_seconds=r.billable_seconds,
            entry_count=r.entry_count,
        )
        for r in rows
    ]


@router.get("/time/report.csv")
async def report_csv(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    group_by: ReportGroup = ReportGroup.project,
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
    billable: bool | None = None,
    executor_kind: ExecKind | None = None,
    client_tag_id: uuid.UUID | None = None,
    project_tag_id: uuid.UUID | None = None,
) -> Response:
    rows = await _report(
        ctx,
        group_by,
        start_from,
        start_to,
        billable,
        executor_kind,
        client_tag_id,
        project_tag_id,
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


@router.get("/time/entries.csv")
async def entries_csv(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    task_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    start_from: datetime.datetime | None = None,
    start_to: datetime.datetime | None = None,
    billable: bool | None = None,
    client_tag_id: uuid.UUID | None = None,
    project_tag_id: uuid.UUID | None = None,
) -> Response:
    """Detail-level CSV: one row per time entry, with started_at /
    ended_at / duration_seconds / task title / client+project / memo.
    Same filter knobs as ``GET /time/entries``; no aggregation."""
    rows = await svc.list_entries(
        ctx.session,
        org_id=ctx.org_id,
        task_id=task_id,
        user_id=user_id,
        start_from=start_from,
        start_to=start_to,
        billable=billable,
        client_tag_id=client_tag_id,
        project_tag_id=project_tag_id,
        limit=None,
        offset=0,
    )
    ctxs = await svc.resolve_task_contexts(ctx.session, [e.task_id for e in rows])
    note_titles = await svc.resolve_note_titles(ctx.session, [e.note_id for e in rows])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "started_at",
            "ended_at",
            "duration_seconds",
            "task_id",
            "task_title",
            "client",
            "project",
            "billable",
            "rate",
            "currency",
            "source",
            "executor_kind",
            "memo",
            "note_id",
            "note_title",
        ]
    )
    for e in rows:
        tctx = ctxs.get(e.task_id, _EMPTY_CTX)
        w.writerow(
            [
                e.started_at.isoformat(),
                e.ended_at.isoformat() if e.ended_at is not None else "",
                e.duration_seconds if e.duration_seconds is not None else "",
                str(e.task_id),
                tctx.task_title or "",
                tctx.client_name or "",
                tctx.project_name or "",
                "yes" if e.billable else "no",
                f"{e.rate_snapshot}" if e.rate_snapshot is not None else "",
                e.currency,
                e.source.value if hasattr(e.source, "value") else str(e.source),
                e.executor_kind.value
                if hasattr(e.executor_kind, "value")
                else str(e.executor_kind),
                (e.memo or "").replace("\n", " ").replace("\r", " "),
                str(e.note_id) if e.note_id is not None else "",
                note_titles.get(e.note_id, "") if e.note_id is not None else "",
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="time-entries.csv"'},
    )
