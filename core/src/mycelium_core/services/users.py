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

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.user import User
from mycelium_core.services.image_validation import (
    IMAGE_MAX_BYTES,
    IMAGE_MIMES,
    image_is_decodable,
)
from mycelium_core.timewindow import resolve_tz

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


SUPPORTED_LANGUAGES = ("en", "it")


def normalize_language(value: str | None) -> str | None:
    """Validate the UI / notification locale. A supported code ("it" /
    "en") is kept; anything else (including None / empty / an unsupported
    tag) clears the preference, which resolves to the default locale
    ("en") when reminder text is rendered."""
    if value is None:
        return None
    code = value.strip().lower()
    return code if code in SUPPORTED_LANGUAGES else None


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
    language: str | None | Any = _UNSET,
) -> User:
    """Patch the caller's reminder profile (timezone / day start /
    language). Only the fields actually passed are touched (the
    ``_UNSET`` sentinel distinguishes "leave alone" from an explicit
    ``None``), so patching one does not clear the others. Caller is
    already authenticated (``current_user``), so the row exists."""
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
    if timezone is not _UNSET:
        user.timezone = normalize_timezone(timezone)
    if day_start_minute is not _UNSET:
        user.day_start_minute = normalize_day_start_minute(day_start_minute)
    if language is not _UNSET:
        user.language = normalize_language(language)
    await session.flush()
    return user


async def set_timezone(session: AsyncSession, *, user_id: uuid.UUID, timezone: str | None) -> User:
    """Set (or clear) the caller's IANA timezone preference."""
    return await update_profile(session, user_id=user_id, timezone=timezone)


async def set_user_avatar(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    data: bytes,
    mime: str,
    seed: str | None = None,
    bg: str | None = None,
    net: str | None = None,
) -> User:
    """Store/replace the caller's avatar PNG/JPEG plus its styling identity
    (the regeneration seed + the two colors). Mirrors the issuer-logo gate
    (shared ``image_validation`` primitives): MIME allowlist, size cap, and a
    full decode-validate so a non-raster blob can never be stored and then
    500 a later render. Self-service: the caller edits their OWN row, so no
    org role is required (auth is the caller being themselves)."""
    if mime not in IMAGE_MIMES:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"avatar mime '{mime}'")
    if not data:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail="empty avatar")
    if len(data) > IMAGE_MAX_BYTES:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail="avatar too large")
    if not image_is_decodable(data):
        raise DomainError(MessageCode.DOMAIN_ERROR, detail="avatar not a decodable image")
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
    user.avatar_data = data
    user.avatar_mime = mime
    user.avatar_seed = seed[:64] if seed else None
    user.avatar_bg = bg[:9] if bg else None
    user.avatar_net = net[:9] if net else None
    user.avatar_updated_at = dt.datetime.now(dt.UTC)
    await session.flush()
    return user


async def get_user_avatar(session: AsyncSession, *, user_id: uuid.UUID) -> tuple[bytes, str] | None:
    """The avatar bytes + mime, or None when unset. Explicit two-column
    select because ``avatar_data`` is a deferred column (never auto-loaded)."""
    row = (
        await session.execute(select(User.avatar_data, User.avatar_mime).where(User.id == user_id))
    ).one_or_none()
    if row is None or row[0] is None:
        return None
    return bytes(row[0]), (row[1] or "application/octet-stream")
