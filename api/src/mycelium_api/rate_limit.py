"""Per-issuer-key rate limiter (task 19b7e874, phase 3b).

A shared-store (Postgres) fixed-window counter: one row per
``(key_id, endpoint_class)``, maintained by a single atomic
``INSERT ... ON CONFLICT DO UPDATE`` that either resets the window (when the
stored one has aged out) or increments the count. Past the class budget it raises
``QuotaExceededError`` -> 429.

The check runs in the request transaction, so it counts committed requests; a
request that later fails rolls its increment back. Once the stored count reaches
the limit, every further request increments to limit+1, raises, and rolls back to
the limit -- so the window stays capped.

Exception (ADR-0046): a transmit-class request that reaches the two-phase
pre-dispatch checkpoint commits its increment there, so a dispatch that later
fails still consumes budget. That is the right accounting -- a real SdI
dispatch attempt was made -- but it means retries through an SdI outage spend
the transmit budget.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core import security_events
from mycelium_core.errors import QuotaExceededError
from mycelium_core.i18n import MessageCode

_UPSERT = text(
    """
    INSERT INTO issuer_key_rate_limit (key_id, endpoint_class, org_id, window_start, count)
    VALUES (:k, :c, :o, now(), 1)
    ON CONFLICT (key_id, endpoint_class) DO UPDATE SET
      window_start = CASE
        WHEN issuer_key_rate_limit.window_start < now() - make_interval(secs => :w)
        THEN now() ELSE issuer_key_rate_limit.window_start END,
      count = CASE
        WHEN issuer_key_rate_limit.window_start < now() - make_interval(secs => :w)
        THEN 1 ELSE issuer_key_rate_limit.count + 1 END
    RETURNING count
    """
)


async def check(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    key_id: uuid.UUID,
    endpoint_class: str,
    limit: int,
    window_seconds: int,
) -> None:
    count = (
        await session.execute(
            _UPSERT,
            {"k": key_id, "c": endpoint_class, "o": org_id, "w": window_seconds},
        )
    ).scalar_one()
    if count > limit:
        # Structured security telemetry (task d3dd69c3): a key hammering its
        # budget is brute-force / runaway-integration signal for the log stack.
        security_events.emit(
            "issuer_key.rate_limited",
            key_id=str(key_id),
            org_id=str(org_id),
            endpoint_class=endpoint_class,
            limit=limit,
        )
        raise QuotaExceededError(MessageCode.RATE_LIMITED)


__all__ = ["check"]
