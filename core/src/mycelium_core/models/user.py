"""Global user (not org-scoped).

Org membership is in ``Membership``. This table is not subject to
tenant RLS: login must be able to resolve the email before having an
org context. Auth-hardening columns (W1b, ported from
bitvision_phoenix; ADR-0024): email verification, TOTP MFA + backup
codes, an optional display name, an admin flag.
"""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, TimestampMixin, UUIDPKMixin


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # Human-readable assignee handle (migration 0060). The assignee
    # picker uses this instead of the UUID. Empty string is the seed
    # sentinel; the service mints a slug on next write. Uniqueness via
    # a partial unique index that ignores the empty sentinel.
    handle: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # IANA timezone (migration 0019), e.g. "Europe/Rome". Drives the
    # local-time rendering of reminder labels and the date-only ("no time
    # set") detection in ``scan_reminders``. NULL = UTC. Captured from the
    # browser on signup/settings; user-overridable.
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Minutes after local midnight (in ``timezone``) that a date-only
    # task's reminders anchor to (migration 0033). 0 = local midnight
    # (start of day, the default); 360 = 06:00. Lets a "due today"
    # reminder fire in the morning instead of at the 23:59:59 end-of-day
    # expiry sentinel (which read as a day late). Expiry/overdue is
    # unaffected -- it still runs at end-of-day.
    day_start_minute: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    # UI / notification locale, "it" | "en" (migration 0034). Drives the
    # language of worker-generated reminder text (no request context, so
    # it can't read Accept-Language). NULL = the default locale ("en").
    # Captured from the SPA's language switcher.
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_admin: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    # Email verification (gated by MYCELIUM_REQUIRE_EMAIL_VERIFICATION).
    email_verified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # TOTP MFA: secret pending until activated; enabled_at stamps
    # activation; backup codes are stored as argon2 hashes.
    mfa_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mfa_enabled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    backup_codes_hash: Mapped[list[str] | None] = mapped_column(ARRAY(Text()), nullable=True)

    # Login lockout (W1b): DB-backed shared state, not per-process.
    failed_login_count: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
