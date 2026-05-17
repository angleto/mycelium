"""Working-calendars router: calendars, holidays, per-user assignment.
Thin adapter over the service layer (docs/adr/0001, 0004, FR-4)."""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    CalendarCreateIn,
    CalendarOut,
    HolidayIn,
    HolidayOut,
    UserCalendarIn,
)
from flow_core.services import calendar as svc

router = APIRouter(tags=["calendars"])


@router.post("/calendars", response_model=CalendarOut)
async def create_calendar(
    body: CalendarCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> CalendarOut:
    cal = await svc.create_calendar(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        name=body.name,
        timezone=body.timezone,
        weekly_hours=body.weekly_hours,
    )
    return CalendarOut(
        id=cal.id,
        name=cal.name,
        is_default=cal.is_default,
        timezone=cal.timezone,
        version=cal.version,
    )


@router.get("/calendars", response_model=list[CalendarOut])
async def list_calendars(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[CalendarOut]:
    rows = await svc.list_calendars(ctx.session, org_id=ctx.org_id)
    return [
        CalendarOut(
            id=c.id,
            name=c.name,
            is_default=c.is_default,
            timezone=c.timezone,
            version=c.version,
        )
        for c in rows
    ]


@router.post("/calendars/{calendar_id}/holidays", status_code=status.HTTP_204_NO_CONTENT)
async def add_holiday(
    calendar_id: uuid.UUID,
    body: HolidayIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.add_holiday(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        calendar_id=calendar_id,
        day=body.day,
    )


@router.get("/calendars/{calendar_id}/holidays", response_model=list[HolidayOut])
async def list_holidays(
    calendar_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[HolidayOut]:
    days = await svc.list_holidays(ctx.session, org_id=ctx.org_id, calendar_id=calendar_id)
    return [HolidayOut(day=d) for d in days]


@router.delete(
    "/calendars/{calendar_id}/holidays/{day}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_holiday(
    calendar_id: uuid.UUID,
    day: datetime.date,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.remove_holiday(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        calendar_id=calendar_id,
        day=day,
    )


@router.put("/users/{user_id}/calendar", status_code=status.HTTP_204_NO_CONTENT)
async def set_user_calendar(
    user_id: uuid.UUID,
    body: UserCalendarIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    await svc.set_user_calendar(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        user_id=user_id,
        calendar_id=body.calendar_id,
        daily_capacity_h=body.daily_capacity_h,
    )
