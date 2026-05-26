"""Task-search embedding backfill loop.

Recovers task blobs that were written keyword-only because the embedder
timed out (2 s cap inside ``services.task_search._safe_embed``). The
listener-driven resync is the authoritative path; this loop is purely a
safety net so a transient embed slowdown doesn't permanently leave
search degraded for those tasks.

Per-workspace, exception-isolated, same shape as ``reminders``: enumerate
orgs under ``admin_session``, then run each backfill tick inside a
``tenant_session`` so RLS scopes the SELECT and the owner is a real
member with role >= member (the service-level call is system-actor).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.models.membership import Membership, Role
from flow_core.models.organization import Organization
from flow_core.services import task_search

_log = logging.getLogger("flow.worker.task_search")


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


async def run_once(batch_size: int = 20) -> int:
    """One sweep across all workspaces. Returns the total number of
    blobs re-embedded. Per-workspace exceptions isolated/logged."""
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("task-search backfill: failed to list workspaces")
        return 0
    total = 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                count = await task_search.run_embedding_backfill(s, batch_size=batch_size)
            if count:
                _log.info("task-search backfill org=%s re-embedded=%d", org_id, count)
            total += count
        except Exception:
            _log.exception("task-search backfill failed for org=%s", org_id)
    return total


async def run_forever() -> None:
    interval = max(5, get_settings().task_search_backfill_interval_seconds)
    _log.info("task-search backfill worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
