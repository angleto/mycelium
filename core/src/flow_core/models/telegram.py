"""Telegram bot integration: per-user single link + single-use codes.

Telegram does not expose a user's ``chat_id`` through any UI surface
or via the bot HTTP API on a username; the only way to obtain it is
to receive a message from that user. So binding a Flow account to a
Telegram chat runs through a deep-link dance (epic #125 P2):

1. The signed-in user clicks "Link Telegram" in the SPA. The backend
   mints a ``TelegramLinkCode`` row (random 8-char hex, 15 min TTL,
   single-use) and returns a ``https://t.me/<bot>?start=<code>`` URL.
2. The user opens the link; Telegram delivers ``/start <code>`` to
   the bot.
3. The bot webhook looks up the code, persists the resulting
   ``chat_id`` + ``username`` in a ``TelegramLink`` row (one per user,
   ``user_id`` PK) and stamps ``consumed_at`` on the code so a replay
   is rejected. The user is then reachable on Telegram for outgoing
   notifications and incoming messages become Notes (or Tasks, with
   the ``/task`` prefix).

Per-user design rationale (versus per-org): the Telegram identity is
attached to a human, not to a workspace. A user with memberships in
multiple workspaces opens Flow on the same Telegram chat regardless
of "which workspace is active right now", so the link is keyed on
``user_id``; we still carry ``org_id`` on link codes so the
"workspace I was in when I requested the link" is auditable and so
RLS scopes the codes' visibility to the workspace that minted them
(a code is just a one-time secret in flight).

Idempotency for incoming updates is enforced by a separate
``TelegramUpdate`` row keyed on Telegram's monotonically increasing
``update_id`` (UNIQUE): replaying the same webhook delivery is a
no-op rather than a double-creating Notes/Tasks. Telegram retries
failed deliveries aggressively, so this is load-bearing.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class TelegramLinkCode(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    """A short-lived, single-use code embedded in the t.me deep link.

    Globally unique on ``code`` (a code in flight is by itself an
    authentication factor: whoever types ``/start <code>`` to the bot
    becomes the link target). Per-user single live code is enforced at
    the service layer (existing unconsumed codes for the same user are
    invalidated on a new mint), not at the DB level: this keeps the
    schema minimal and the audit trail intact (a consumed/expired code
    row is kept for accountability)."""

    __tablename__ = "telegram_link_codes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Set by the webhook on a successful ``/start <code>``; once set
    # the code is dead and further redemptions are rejected.
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("code", name="uq_telegram_link_codes_code"),
        # Per-user pending lookup ("does this user have a live code?")
        # for the polling SPA, partial-index style.
        Index(
            "ix_telegram_link_codes_user_pending",
            "user_id",
            postgresql_where="consumed_at IS NULL",
        ),
        CheckConstraint(
            # Hex/base32-ish: keep a reasonable bound (the mint draws
            # 8 hex chars; 32 leaves room for later format changes).
            "length(code) BETWEEN 6 AND 32",
            name="ck_telegram_link_codes_length",
        ),
    )


class TelegramLink(TimestampMixin, VersionMixin, Base):
    """One Telegram identity per Flow user. ``user_id`` is the PK so
    re-linking is an UPSERT (a user picks ONE chat at a time). The
    ``chat_id`` is BIGINT because Telegram's chat IDs are int64
    (negative for groups, positive for users / channels).

    Not OrgScoped: the link belongs to the human, not the workspace
    (see module docstring). RLS still applies (the table is FORCE-RLS
    in the migration) but the policy keys on the caller's user_id GUC
    rather than the workspace, mirroring the ``p_memberships_self_read``
    pattern from migration 0051."""

    __tablename__ = "telegram_links"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The Telegram-side username (``@handle``) when the user has one
    # set; absent for users without a public username. Mirrored from
    # the webhook payload at link time; updated on every incoming
    # message if it changed (Telegram lets users rename).
    chat_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linked_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Stops two Flow users from racing onto the same Telegram chat
        # (a chat that has already been used to link a different user
        # has to be unlinked there before it can move).
        UniqueConstraint("chat_id", name="uq_telegram_links_chat_id"),
    )


class TelegramUpdate(Base):
    """One row per processed Telegram update. The bot HTTP API delivers
    each update with a strictly increasing ``update_id``; persisting it
    UNIQUE-keyed gives us exactly-once handling at the webhook entry
    (Telegram retries on a non-2xx, so this is load-bearing).

    Not OrgScoped + no FK: at the moment the webhook receives an
    update we may not yet know the user (the link is being established
    right now) and the table acts as a process-level seen-set, not as
    tenant data. RLS denies all access from a tenant session (no
    policy granted, so FORCE-RLS hides every row); only the bot
    webhook context (admin session) ever reads/writes it."""

    __tablename__ = "telegram_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["TelegramLink", "TelegramLinkCode", "TelegramUpdate"]
