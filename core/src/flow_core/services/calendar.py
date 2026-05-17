"""Working-time calendar engine + service (FR-4).

The scheduler (F3.4) walks working time over a calendar: weekly
windows, holidays, timezone. ``WorkCalendar`` is a pure, deterministic
helper; the service loads/persists calendars and per-user assignment.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.calendar import (
    CalendarHoliday,
    UserCalendar,
    WorkingCalendar,
)
from flow_core.models.membership import Role
from flow_core.services import audit
from flow_core.services.rbac import require_role

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MAX_DAYS = 4000  # ~10y guard against runaway loops


def _parse_time(value: str) -> dt.time:
    hh, mm = value.split(":")
    return dt.time(hour=int(hh), minute=int(mm))


@dataclass(frozen=True)
class WorkCalendar:
    tz: ZoneInfo
    # weekday index (Mon=0) -> ordered list of (start, end) local times
    windows: dict[int, list[tuple[dt.time, dt.time]]]
    holidays: frozenset[dt.date]

    @classmethod
    def build(
        cls,
        timezone: str,
        weekly_hours: dict[str, Any],
        holidays: frozenset[dt.date],
    ) -> WorkCalendar:
        windows: dict[int, list[tuple[dt.time, dt.time]]] = {}
        for idx, key in enumerate(_WEEKDAYS):
            spans = weekly_hours.get(key, []) or []
            windows[idx] = [(_parse_time(a), _parse_time(b)) for a, b in spans]
        return cls(tz=ZoneInfo(timezone), windows=windows, holidays=holidays)

    def _windows_on(self, day: dt.date) -> list[tuple[dt.datetime, dt.datetime]]:
        if day in self.holidays:
            return []
        out: list[tuple[dt.datetime, dt.datetime]] = []
        for s, e in self.windows.get(day.weekday(), []):
            out.append(
                (
                    dt.datetime.combine(day, s, self.tz),
                    dt.datetime.combine(day, e, self.tz),
                )
            )
        return out

    def snap_forward(self, ts: dt.datetime) -> dt.datetime:
        """Earliest working instant >= ts."""
        loc = ts.astimezone(self.tz)
        day = loc.date()
        for _ in range(_MAX_DAYS):
            for ws, we in self._windows_on(day):
                if loc <= ws:
                    return ws.astimezone(dt.UTC)
                if ws < loc < we:
                    return loc.astimezone(dt.UTC)
            day = day + dt.timedelta(days=1)
            loc = dt.datetime.combine(day, dt.time.min, self.tz)
        raise DomainError(MessageCode.DOMAIN_ERROR)

    def snap_backward(self, ts: dt.datetime) -> dt.datetime:
        """Latest working instant <= ts."""
        loc = ts.astimezone(self.tz)
        day = loc.date()
        for _ in range(_MAX_DAYS):
            for ws, we in reversed(self._windows_on(day)):
                if loc >= we:
                    return we.astimezone(dt.UTC)
                if ws < loc < we:
                    return loc.astimezone(dt.UTC)
            day = day - dt.timedelta(days=1)
            loc = dt.datetime.combine(day, dt.time.max, self.tz)
        raise DomainError(MessageCode.DOMAIN_ERROR)

    def add(self, start: dt.datetime, minutes: int) -> dt.datetime:
        """Advance ``minutes`` of working time from ``start`` (negative
        goes backward; calendar-aware lag/lead)."""
        if minutes < 0:
            return self._subtract(start, -minutes)
        cur = self.snap_forward(start).astimezone(self.tz)
        remaining = float(minutes)
        day = cur.date()
        for _ in range(_MAX_DAYS):
            for ws, we in self._windows_on(day):
                if cur >= we:
                    continue
                seg_start = cur if cur > ws else ws
                if seg_start >= we:
                    continue
                avail = (we - seg_start).total_seconds() / 60.0
                if remaining <= avail:
                    return (seg_start + dt.timedelta(minutes=remaining)).astimezone(dt.UTC)
                remaining -= avail
                cur = we
            day = day + dt.timedelta(days=1)
            cur = dt.datetime.combine(day, dt.time.min, self.tz)
        raise DomainError(MessageCode.DOMAIN_ERROR)

    def _subtract(self, start: dt.datetime, minutes: int) -> dt.datetime:
        cur = self.snap_backward(start).astimezone(self.tz)
        remaining = float(minutes)
        day = cur.date()
        for _ in range(_MAX_DAYS):
            for ws, we in reversed(self._windows_on(day)):
                if cur <= ws:
                    continue
                seg_end = cur if cur < we else we
                if seg_end <= ws:
                    continue
                avail = (seg_end - ws).total_seconds() / 60.0
                if remaining <= avail:
                    return (seg_end - dt.timedelta(minutes=remaining)).astimezone(dt.UTC)
                remaining -= avail
                cur = ws
            day = day - dt.timedelta(days=1)
            cur = dt.datetime.combine(day, dt.time.max, self.tz)
        raise DomainError(MessageCode.DOMAIN_ERROR)


async def _holidays(session: AsyncSession, calendar_id: uuid.UUID) -> frozenset[dt.date]:
    rows = (
        (
            await session.execute(
                select(CalendarHoliday.day).where(CalendarHoliday.calendar_id == calendar_id)
            )
        )
        .scalars()
        .all()
    )
    return frozenset(rows)


async def get_default_calendar(session: AsyncSession, org_id: uuid.UUID) -> WorkingCalendar:
    cal = (
        await session.execute(select(WorkingCalendar).where(WorkingCalendar.is_default.is_(True)))
    ).scalar_one_or_none()
    if cal is None:
        raise DomainError(MessageCode.CALENDAR_NOT_FOUND)
    return cal


async def build_work_calendar(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> tuple[WorkCalendar, Decimal]:
    """Resolve the effective calendar for a user (per-user override
    else Org default) and return the engine + daily capacity hours."""
    calendar = await get_default_calendar(session, org_id)
    capacity = Decimal(8)
    if user_id is not None:
        row = (
            await session.execute(select(UserCalendar).where(UserCalendar.user_id == user_id))
        ).scalar_one_or_none()
        if row is not None:
            capacity = row.daily_capacity_h
            uc = (
                await session.execute(
                    select(WorkingCalendar).where(WorkingCalendar.id == row.calendar_id)
                )
            ).scalar_one_or_none()
            if uc is not None:
                calendar = uc
    holidays = await _holidays(session, calendar.id)
    return (
        WorkCalendar.build(calendar.timezone, calendar.weekly_hours, holidays),
        capacity,
    )


async def create_calendar(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    timezone: str = "Europe/Rome",
    weekly_hours: dict[str, Any],
) -> WorkingCalendar:
    await require_role(session, org_id, actor_id, Role.admin)
    cal = WorkingCalendar(
        org_id=org_id,
        name=name,
        is_default=False,
        timezone=timezone,
        weekly_hours=weekly_hours,
    )
    try:
        async with session.begin_nested():
            session.add(cal)
            await session.flush()
    except IntegrityError as exc:
        raise DomainError(MessageCode.DOMAIN_ERROR) from exc
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="calendar",
        entity_id=cal.id,
        action="create",
    )
    return cal


async def list_calendars(session: AsyncSession, *, org_id: uuid.UUID) -> list[WorkingCalendar]:
    return list(
        (await session.execute(select(WorkingCalendar).order_by(WorkingCalendar.name)))
        .scalars()
        .all()
    )


async def set_user_calendar(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    calendar_id: uuid.UUID,
    daily_capacity_h: Decimal,
) -> None:
    await require_role(session, org_id, actor_id, Role.admin)
    existing = (
        await session.execute(select(UserCalendar).where(UserCalendar.user_id == user_id))
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            UserCalendar(
                org_id=org_id,
                user_id=user_id,
                calendar_id=calendar_id,
                daily_capacity_h=daily_capacity_h,
            )
        )
    else:
        await session.execute(
            update(UserCalendar)
            .where(UserCalendar.user_id == user_id)
            .values(
                calendar_id=calendar_id,
                daily_capacity_h=daily_capacity_h,
                version=UserCalendar.version + 1,
            )
        )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="user_calendar",
        entity_id=user_id,
        action="set",
    )


async def add_holiday(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    calendar_id: uuid.UUID,
    day: dt.date,
) -> None:
    await require_role(session, org_id, actor_id, Role.admin)
    try:
        async with session.begin_nested():
            session.add(CalendarHoliday(org_id=org_id, calendar_id=calendar_id, day=day))
            await session.flush()
    except IntegrityError:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="calendar",
        entity_id=calendar_id,
        action="add_holiday",
    )


async def list_holidays(
    session: AsyncSession, *, org_id: uuid.UUID, calendar_id: uuid.UUID
) -> list[dt.date]:
    return list(
        (
            await session.execute(
                select(CalendarHoliday.day)
                .where(CalendarHoliday.calendar_id == calendar_id)
                .order_by(CalendarHoliday.day)
            )
        )
        .scalars()
        .all()
    )


async def remove_holiday(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    calendar_id: uuid.UUID,
    day: dt.date,
) -> None:
    await require_role(session, org_id, actor_id, Role.admin)
    await session.execute(
        delete(CalendarHoliday).where(
            CalendarHoliday.calendar_id == calendar_id,
            CalendarHoliday.day == day,
        )
    )
