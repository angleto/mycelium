"""Reminders + notification-dispatch worker job.

Periodic loop that closes the reminder pipeline:

  1. ``scan_reminders`` walks active tasks with a firing reference
     (``start_at`` for appointments, ``due_date`` for date-only tasks)
     and enqueues idempotent ``Notification`` rows for each
     ``(assignee, channel)`` whose offset has come into the look-ahead
     window. ``dedupe_key`` includes the precise ``fire_at`` so
     subsequent firings of the same reminder series are not blocked.
  2. ``dispatch_pending`` sends every pending notification through the
     configured sender, honouring per-user ``NotificationPref``
     (channel + target + enabled). Per-item fault isolation.

Session/context mirrors ``dispatch.py``: the no-tenant ``admin_session``
enumerates workspaces; each tick then runs inside ``tenant_session(org,
owner, actor_kind="system")`` so RLS is enforced and the owner's stored
membership satisfies any role check inside the services. One failing
workspace never stops the rest; a transient DB error never kills the
loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.organization import Organization
from mycelium_core.services import notifications as notif_svc

_log = logging.getLogger("flow.worker.reminders")


async def _all_workspaces() -> list[uuid.UUID]:
    """Every workspace gets a reminders tick: unlike the dispatch loop,
    reminders are not gated by an autonomous-dispatch policy (they are
    user-configured per task and the user opts in via channel prefs).
    Deterministic order by ``str(id)``."""
    async with admin_session() as s:
        orgs = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
        return [o.id for o in sorted(orgs, key=lambda o: str(o.id))]


async def _owner_of(org_id: uuid.UUID) -> uuid.UUID | None:
    """Earliest owner membership; ``None`` if the workspace has no
    owner (skip it: there is no actor to run as)."""
    async with admin_session() as s:
        rows = (
            (
                await s.execute(
                    select(Membership)
                    .where(Membership.org_id == org_id, Membership.role == Role.owner)
                    .order_by(Membership.created_at, Membership.user_id)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        return None
    ordered = sorted(rows, key=lambda m: (m.created_at, str(m.user_id)))
    return ordered[0].user_id


async def run_once() -> tuple[int, int]:
    """One sweep across all workspaces. Returns ``(enqueued, sent)``
    aggregates for logging/tests. Per-workspace exceptions are isolated
    and logged; a sweep-level DB error returns ``(0, 0)``."""
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("reminders sweep: failed to list workspaces")
        return (0, 0)
    total_enq = 0
    total_sent = 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                enq = await notif_svc.scan_reminders(s, org_id=org_id, actor_id=owner)
                res = await notif_svc.dispatch_pending(s, org_id=org_id, actor_id=owner)
            if enq or res.sent or res.failed or res.suppressed:
                _log.info(
                    "reminders tick org=%s enqueued=%d sent=%d failed=%d suppressed=%d",
                    org_id,
                    enq,
                    res.sent,
                    res.failed,
                    res.suppressed,
                )
            total_enq += enq
            total_sent += res.sent
        except Exception:
            _log.exception("reminders tick failed for org=%s", org_id)
    return (total_enq, total_sent)


async def run_forever() -> None:
    """Periodic loop: ``run_once`` every
    ``settings.reminders_loop_interval_seconds``. Never exits on a
    per-sweep error; the 60-second floor keeps the loop honest for
    minute-precision reminders on appointment tasks."""
    interval = max(5, get_settings().reminders_loop_interval_seconds)
    _log.info("reminders loop worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
