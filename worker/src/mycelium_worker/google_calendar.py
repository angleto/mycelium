"""Periodic Google Calendar sync worker (epic #125 P1).

Periodic half of the Google Calendar ingest: on a modest interval, every
``active`` ``CalendarSubscription`` gets one ``sync_subscription`` tick.
Per-subscription exception-isolated (one failing subscription does not
stop the others); per-sweep exception-isolated (a transient DB error
does not kill the loop). Mirrors the structure of ``mycelium_worker.dispatch``.

Skipped entirely when Google OAuth is not configured (the prod
``settings.google_configured`` gate): no point trying to refresh
tokens with no client_id / client_secret.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.google_calendar import (
    CalendarSubscription,
    GoogleCalendarStatus,
)
from mycelium_core.services import google_calendar as gcal_svc

_log = logging.getLogger("flow.worker.google_calendar")


async def _active_subscriptions() -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """(subscription_id, org_id, user_id) for every active subscription.
    Read with the no-tenant ``admin_session``: RLS hides nothing here
    because we are an out-of-band cross-tenant scan and the worker
    re-enters a tenant_session for the actual sync."""
    async with admin_session() as s:
        rows = (
            (
                await s.execute(
                    select(
                        CalendarSubscription.id,
                        CalendarSubscription.org_id,
                        CalendarSubscription.user_id,
                    )
                    .where(CalendarSubscription.status == GoogleCalendarStatus.active)
                    .order_by(CalendarSubscription.created_at)
                )
            )
            .tuples()
            .all()
        )
        return [(r[0], r[1], r[2]) for r in rows]


async def run_once() -> int:
    """One sweep. Returns the number of subscriptions that ingested at
    least one event (for logging / tests)."""
    if not get_settings().google_configured:
        return 0
    touched = 0
    try:
        subs = await _active_subscriptions()
    except Exception:
        _log.exception("google calendar sweep: failed to list subscriptions")
        return 0
    for sub_id, org_id, user_id in subs:
        try:
            async with tenant_session(str(org_id), str(user_id), actor_kind="system") as s:
                res = await gcal_svc.sync_subscription(
                    s,
                    org_id=org_id,
                    actor_id=user_id,
                    subscription_id=sub_id,
                )
            if res.ingested or res.updated:
                _log.info(
                    "google calendar sync subscription=%s ingested=%d updated=%d skipped=%d",
                    sub_id,
                    res.ingested,
                    res.updated,
                    res.skipped,
                )
                touched += 1
            elif not res.ok:
                _log.warning(
                    "google calendar sync subscription=%s failed: %s",
                    sub_id,
                    res.error,
                )
        except Exception:
            _log.exception("google calendar sync failed for subscription=%s", sub_id)
    return touched


async def run_forever() -> None:
    interval = max(30, get_settings().google_calendar_sync_interval_seconds)
    _log.info("google calendar sync worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
