"""Webhook delivery worker job (task 2c23e955, ADR-0047).

Decoupled drain of the ``webhook_deliveries`` outbox. Each tick sweeps every
workspace inside ``tenant_session(org, owner, actor_kind='system')`` (RLS
enforced, one failing workspace never stops the rest) and:

  1. ``deliver_due`` reclaims expired leases, claims a bounded batch of due
     rows (``FOR UPDATE SKIP LOCKED``), POSTs each signed payload, and records
     the outcome with exponential backoff.
  2. ``purge_expired_deliveries`` drops terminal rows past the retention window.

Registered only when ``settings.webhooks_enabled`` (see worker ``main``); a
disabled deploy never runs the loop, so the fiscal path is untouched.
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
from mycelium_core.services import webhooks as webhooks_svc

_log = logging.getLogger("mycelium.worker.webhooks")


async def _all_workspaces() -> list[uuid.UUID]:
    async with admin_session() as s:
        orgs = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
        return [o.id for o in sorted(orgs, key=lambda o: str(o.id))]


async def _owner_of(org_id: uuid.UUID) -> uuid.UUID | None:
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
    """One drain sweep across all workspaces. Returns ``(delivered, failed)``.
    Per-workspace exceptions are isolated; a sweep-level DB error returns
    ``(0, 0)`` without killing the loop."""
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("webhooks sweep: failed to list workspaces")
        return (0, 0)
    total_delivered = 0
    total_failed = 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                delivered, failed = await webhooks_svc.deliver_due(s, org_id=org_id)
                purged = await webhooks_svc.purge_expired_deliveries(s, org_id=org_id)
            if delivered or failed or purged:
                _log.info(
                    "webhooks tick org=%s delivered=%d failed=%d purged=%d",
                    org_id,
                    delivered,
                    failed,
                    purged,
                )
            total_delivered += delivered
            total_failed += failed
        except Exception:
            _log.exception("webhooks tick failed for org=%s", org_id)
    return (total_delivered, total_failed)


async def run_forever() -> None:
    interval = max(5, get_settings().webhook_poll_interval_seconds)
    _log.info("webhooks loop worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
