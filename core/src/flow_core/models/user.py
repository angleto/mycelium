"""Global user (not org-scoped).

Org membership is in ``Membership``. This table is not subject to
tenant RLS: login must be able to resolve the email before having an
org context. Auth-hardening columns (W1b, ported from
bitvision_phoenix; ADR-0024): email verification, TOTP MFA + backup
codes, an optional display name, an admin flag.
"""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, TimestampMixin, UUIDPKMixin


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
    is_admin: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    # Email verification (gated by FLOW_REQUIRE_EMAIL_VERIFICATION).
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
