"""User-profile mutations not tied to auth or org membership.

Currently just the IANA timezone preference used to render reminder
labels in local time and to detect the date-only sentinel in the user's
own timezone (``services.notifications.scan_reminders``). The users table
is global (no tenant RLS), so writes go through the no-tenant admin
session, like the auth flows.
"""

from __future__ import annotations

import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.user import User


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


async def set_timezone(session: AsyncSession, *, user_id: uuid.UUID, timezone: str | None) -> User:
    """Set (or clear) the caller's IANA timezone preference. Caller has
    already been authenticated (``current_user``), so the row exists."""
    tz = normalize_timezone(timezone)
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
    user.timezone = tz
    await session.flush()
    return user
