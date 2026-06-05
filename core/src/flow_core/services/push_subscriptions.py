"""Browser Web Push subscription CRUD (channel=webpush).

The SPA subscribes the browser's PushManager with our VAPID public key and
POSTs the resulting subscription here; the reminder dispatcher reads these
rows and fans a webpush notification out to each of a user's devices.
Org-scoped via RLS; idempotent on the endpoint so a re-subscribe refreshes
keys instead of duplicating.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.push_subscription import PushSubscription
from flow_core.services import audit


async def upsert_subscription(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    endpoint: str,
    p256dh: str,
    auth: str,
) -> PushSubscription:
    """Idempotent on (org_id, endpoint): a browser re-subscribing with the
    same endpoint refreshes its keys / owner rather than duplicating."""
    row = (
        await session.execute(
            select(PushSubscription).where(
                PushSubscription.org_id == org_id,
                PushSubscription.endpoint == endpoint,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = PushSubscription(
            org_id=org_id, user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth
        )
        session.add(row)
    else:
        row.user_id = user_id
        row.p256dh = p256dh
        row.auth = auth
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="push_subscription",
        entity_id=row.id,
        action="subscribe",
        diff={},
    )
    return row


async def list_subscriptions(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> Sequence[PushSubscription]:
    return (
        (
            await session.execute(
                select(PushSubscription)
                .where(
                    PushSubscription.org_id == org_id,
                    PushSubscription.user_id == user_id,
                )
                .order_by(PushSubscription.created_at)
            )
        )
        .scalars()
        .all()
    )


async def delete_subscription(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    endpoint: str,
) -> bool:
    """Remove the caller's subscription for ``endpoint`` (browser unsubscribe
    or a stale row). Returns True if a row was deleted."""
    row = (
        await session.execute(
            select(PushSubscription).where(
                PushSubscription.org_id == org_id,
                PushSubscription.user_id == user_id,
                PushSubscription.endpoint == endpoint,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="push_subscription",
        entity_id=None,
        action="unsubscribe",
        diff={},
    )
    return True
