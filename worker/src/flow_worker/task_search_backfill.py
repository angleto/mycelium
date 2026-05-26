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


async def run_once(batch_size: int = 20, pointer_batch_size: int = 50) -> tuple[int, int]:
    """One sweep across all workspaces.

    Returns ``(re_embedded, indexed)``:
    - ``re_embedded``: blobs whose initial write timed out and that we
      just re-embedded.
    - ``indexed``: tasks that pre-date the task-search deploy (no
      pointer yet) and that we just indexed via ``run_pointer_backfill``.

    Per-workspace exceptions isolated/logged.
    """
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("task-search backfill: failed to list workspaces")
        return (0, 0)
    total_re_embedded = 0
    total_indexed = 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                re_embedded = await task_search.run_embedding_backfill(s, batch_size=batch_size)
                indexed = await task_search.run_pointer_backfill(s, batch_size=pointer_batch_size)
            if re_embedded or indexed:
                _log.info(
                    "task-search backfill org=%s re-embedded=%d indexed=%d",
                    org_id,
                    re_embedded,
                    indexed,
                )
            total_re_embedded += re_embedded
            total_indexed += indexed
        except Exception:
            _log.exception("task-search backfill failed for org=%s", org_id)
    return (total_re_embedded, total_indexed)


async def run_forever() -> None:
    interval = max(5, get_settings().task_search_backfill_interval_seconds)
    _log.info("task-search backfill worker started (interval=%ds)", interval)
    # Boost the first sweep: workspaces that pre-date the task-search
    # deploy can carry hundreds of unindexed tasks; the default
    # ``pointer_batch_size`` of 50 would take ~30 ticks (~30 min) to
    # drain. The boost ticks finish the migration in a couple of
    # minutes, then the loop settles back to the small steady-state
    # batch which is enough for normal mutation drift.
    boost_pointer_batch = 500
    await run_once(pointer_batch_size=boost_pointer_batch)
    while True:
        await asyncio.sleep(interval)
        await run_once()
