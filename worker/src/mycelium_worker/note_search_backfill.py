"""Note-search pointer backfill loop.

Indexes note parts that pre-date the per-part index deploy (migration
0040): the listener-driven resync catches every new mutation, but parts
created before the deploy never went through it, so an old note stays
invisible to semantic retrieval until it is edited. This sweep walks
those parts and runs the same ``_resync_part_blob`` the listener would
have, so the back-catalogue becomes searchable on its own.

Unlike ``task_search_backfill`` there is no separate embedding-backfill
call here: keyword-only blobs (embedder timed out) are re-embedded
generically by the ``embedding_migration`` worker, which re-embeds any
blob with a NULL vector regardless of channel.

Per-workspace, exception-isolated, same shape as ``task_search_backfill``:
enumerate orgs under ``admin_session``, then run each backfill tick inside
a ``tenant_session`` so RLS scopes the SELECT and the owner is a real
member with role >= member (the service-level call is system-actor).
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
from mycelium_core.services import note_search

_log = logging.getLogger("mycelium.worker.note_search")


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


async def run_once(pointer_batch_size: int = 50) -> int:
    """One sweep across all workspaces.

    Returns ``indexed``: note parts that pre-date the per-part index
    deploy (no pointer yet) and that we just indexed via
    ``run_pointer_backfill``. Per-workspace exceptions isolated/logged.
    """
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("note-search backfill: failed to list workspaces")
        return 0
    total_indexed = 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                indexed = await note_search.run_pointer_backfill(s, batch_size=pointer_batch_size)
            if indexed:
                _log.info("note-search backfill org=%s indexed=%d", org_id, indexed)
            total_indexed += indexed
        except Exception:
            _log.exception("note-search backfill failed for org=%s", org_id)
    return total_indexed


async def run_forever() -> None:
    interval = max(5, get_settings().note_search_backfill_interval_seconds)
    _log.info("note-search backfill worker started (interval=%ds)", interval)
    # Boost the first sweep: workspaces that pre-date the per-part index
    # can carry hundreds of unindexed note parts; the default
    # ``pointer_batch_size`` of 50 would take many ticks to drain. The
    # boost tick finishes the migration in a couple of minutes, then the
    # loop settles back to the small steady-state batch which is enough
    # for the rare part that slips past the listener.
    boost_pointer_batch = 500
    await run_once(pointer_batch_size=boost_pointer_batch)
    while True:
        await asyncio.sleep(interval)
        await run_once()
