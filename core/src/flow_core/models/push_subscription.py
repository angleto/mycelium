"""Browser Web Push subscriptions (RFC 8030/8291).

One row per (user, device/browser) push endpoint. Unlike email/telegram a
user has MANY subscriptions, so the channel pref (``notification_prefs``
with ``channel='webpush'``) is just the on/off switch and the reminder
dispatcher fans out to every subscription here. A subscription is pruned
when the push service reports the endpoint gone (404/410) at send time.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, OrgScopedMixin, TimestampMixin, UUIDPKMixin


class PushSubscription(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "push_subscriptions"
    __table_args__ = (
        UniqueConstraint("org_id", "endpoint", name="uq_push_subscriptions_org_endpoint"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The push service URL the browser handed us (RFC 8030). Long: FCM /
    # Mozilla autopush endpoints routinely exceed 512 chars.
    endpoint: Mapped[str] = mapped_column(String(2048), nullable=False)
    # The subscription's public key (p256dh) and auth secret, base64url;
    # pywebpush encrypts the payload to these (RFC 8291).
    p256dh: Mapped[str] = mapped_column(String(256), nullable=False)
    auth: Mapped[str] = mapped_column(String(256), nullable=False)
