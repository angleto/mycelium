"""Notifications / recurrence / reminders router. Thin adapter
(docs/adr/0001, FR-12). The sender is injected (fake in tests);
dispatch/recurrence/reminder logic lives in the service."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    CountOut,
    DispatchOut,
    NotificationOut,
    NotificationPrefIn,
    NotificationPrefOut,
    RecurrenceIn,
    RecurrenceOut,
)
from flow_core.models.notification import Notification, NotificationPref, TaskRecurrence
from flow_core.services import notifications as svc

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _pref_out(p: NotificationPref) -> NotificationPrefOut:
    return NotificationPrefOut(
        user_id=p.user_id, channel=p.channel, enabled=p.enabled, target=p.target
    )


def _n_out(n: Notification) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        user_id=n.user_id,
        channel=n.channel,
        kind=n.kind,
        title=n.title,
        body=n.body,
        status=n.status,
        created_at=n.created_at,
        sent_at=n.sent_at,
    )


def _rec_out(r: TaskRecurrence) -> RecurrenceOut:
    return RecurrenceOut(
        task_id=r.task_id,
        freq=r.freq,
        interval=r.interval,
        next_run=r.next_run,
        until=r.until,
        active=r.active,
    )


@router.put("/prefs", response_model=NotificationPrefOut)
async def set_pref(
    body: NotificationPrefIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> NotificationPrefOut:
    p = await svc.set_pref(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        user_id=body.user_id,
        channel=body.channel,
        enabled=body.enabled,
        target=body.target,
    )
    return _pref_out(p)


@router.get("/prefs", response_model=list[NotificationPrefOut])
async def list_prefs(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[NotificationPrefOut]:
    rows = await svc.list_prefs(ctx.session, org_id=ctx.org_id, user_id=ctx.user_id)
    return [_pref_out(p) for p in rows]


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[NotificationOut]:
    rows = await svc.list_notifications(ctx.session, org_id=ctx.org_id, user_id=ctx.user_id)
    return [_n_out(n) for n in rows]


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification(
    notification_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.delete_notification(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        notification_id=notification_id,
    )


@router.post("/dispatch", response_model=DispatchOut)
async def dispatch(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> DispatchOut:
    r = await svc.dispatch_pending(ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id)
    return DispatchOut(sent=r.sent, failed=r.failed)


@router.post("/recurrences", response_model=RecurrenceOut)
async def create_recurrence(
    body: RecurrenceIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> RecurrenceOut:
    rec = await svc.create_recurrence(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=body.task_id,
        freq=body.freq,
        next_run=body.next_run,
        interval=body.interval,
        until=body.until,
    )
    return _rec_out(rec)


@router.post("/recurrences/spawn-due", response_model=CountOut)
async def spawn_due(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> CountOut:
    n = await svc.spawn_due(ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id)
    return CountOut(count=n)


@router.post("/reminders/scan", response_model=CountOut)
async def scan_reminders(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    within_days: int = 1,
) -> CountOut:
    n = await svc.scan_reminders(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        within_days=within_days,
    )
    return CountOut(count=n)
