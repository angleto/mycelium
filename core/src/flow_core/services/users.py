"""User-profile mutations not tied to auth or org membership.

The per-user reminder preferences: the IANA timezone (renders reminder
labels in local time, detects the date-only sentinel, and -- via
``get_user_tz`` -- anchors a date-only deadline to end-of-day in the
owner's own day) and ``day_start_minute`` (the minute after local
midnight a date-only task's reminders fire, so a "due today" reminder
lands in the morning rather than at 23:59). The users table is global
(no tenant RLS), so writes go through the no-tenant admin session, like
the auth flows.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.user import User
from flow_core.timewindow import resolve_tz

# Sentinel for "field not provided" in ``update_profile`` -- distinct
# from ``None``, which is a meaningful value (clear the timezone /
# reset the day start to its default).
_UNSET: Any = object()


def normalize_timezone(value: str | None) -> str | None:
    """Validate an IANA timezone name. Empty / None clears the preference
    (resolves to UTC). An unrecognised name raises
    ``USER_TIMEZONE_INVALID`` with the offending value as detail
    (docs/adr/0017: no hardcoded prose)."""
    if value is None:
        return None
    name = value.strip()
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainError(MessageCode.USER_TIMEZONE_INVALID, detail=name) from exc
    return name


def normalize_day_start_minute(value: int | None) -> int:
    """Validate the per-user day-start offset (minutes after local
    midnight). None resets to 0 (start of day). Out of the 0..1439 range
    raises ``USER_DAY_START_INVALID`` with the offending value."""
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1439:
        raise DomainError(MessageCode.USER_DAY_START_INVALID, detail=str(value))
    return value


async def get_user_tz(session: AsyncSession, *, user_id: uuid.UUID) -> dt.tzinfo:
    """The user's configured IANA timezone as a tzinfo (UTC when unset
    or unknown). The single source of truth for anchoring a date-only
    deadline to the end of *that user's* calendar day."""
    name = (
        await session.execute(select(User.timezone).where(User.id == user_id))
    ).scalar_one_or_none()
    return resolve_tz(name)


async def update_profile(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    timezone: str | None | Any = _UNSET,
    day_start_minute: int | None | Any = _UNSET,
) -> User:
    """Patch the caller's reminder profile. Only the fields actually
    passed are touched (the ``_UNSET`` sentinel distinguishes "leave
    alone" from an explicit ``None``), so patching the day start does not
    clear the timezone and vice-versa. Caller is already authenticated
    (``current_user``), so the row exists."""
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
    if timezone is not _UNSET:
        user.timezone = normalize_timezone(timezone)
    if day_start_minute is not _UNSET:
        user.day_start_minute = normalize_day_start_minute(day_start_minute)
    await session.flush()
    return user


async def set_timezone(session: AsyncSession, *, user_id: uuid.UUID, timezone: str | None) -> User:
    """Set (or clear) the caller's IANA timezone preference."""
    return await update_profile(session, user_id=user_id, timezone=timezone)
