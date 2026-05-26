"""Embedding migration worker loop (task `1d081395`).

Per-workspace sweep that backfills ``embedding_v2`` via the
configured v2 model. Same shape as the task-search backfill:
``admin_session`` enumerates orgs, ``tenant_session`` per org for
RLS, exception-isolated, periodic.

The whole loop is a no-op when ``FLOW_EMBED_MODEL_V2`` is empty
(no migration target configured): saves the per-org churn on a
fresh deployment that hasn't opted in.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.embedder import get_embedder_v2
from flow_core.models.membership import Membership, Role
from flow_core.models.organization import Organization
from flow_core.services import embedding_migration as svc

_log = logging.getLogger("flow.worker.embedding_migration")


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


async def run_once(batch_size: int = 50) -> int:
    """One sweep across all workspaces. Returns total blobs migrated.
    Skips entirely when no v2 model is configured."""
    if get_embedder_v2() is None:
        return 0
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("embedding migration: failed to list workspaces")
        return 0
    total = 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                count = await svc.run_embedding_migration(s, batch_size=batch_size)
            if count:
                _log.info("embedding migration org=%s migrated=%d", org_id, count)
            total += count
        except Exception:
            _log.exception("embedding migration failed for org=%s", org_id)
    return total


async def run_forever() -> None:
    interval = max(5, get_settings().embedding_migration_interval_seconds)
    _log.info(
        "embedding migration worker started (interval=%ds, model_v2=%s)",
        interval,
        get_settings().embed_model_v2 or "<not configured>",
    )
    while True:
        await run_once()
        await asyncio.sleep(interval)
