"""Embedding backfill worker loop (task 5276207e).

Per-workspace sweep that backfills both embedding tiers (local
``embedding`` + the optional hosted ``embedding_hosted``) for rows
missing a vector. Same shape as the task-search backfill:
``admin_session`` enumerates orgs, ``tenant_session`` per org for RLS
(so ``resolve_hosted_embedder`` sees the org's config), exception-
isolated, periodic. A batch with nothing to do is a cheap empty SELECT.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.embedder import embedder_available
from flow_core.models.membership import Membership, Role
from flow_core.models.organization import Organization
from flow_core.services import autonomous_budget
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
    """One sweep across all workspaces. Returns total rows re-embedded."""
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("embedding backfill: failed to list workspaces")
        return 0
    total = 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                # WS-F5: re-embedding is autonomous spend; skip a workspace
                # whose kill-switch is off or daily cap is reached.
                bstatus = await autonomous_budget.status(s, org_id=org_id)
                if bstatus.paused:
                    _log.info("embedding backfill paused org=%s reason=%s", org_id, bstatus.reason)
                    continue
                count = await svc.run_embedding_backfill(s, org_id, batch_size=batch_size)
            if count:
                _log.info("embedding backfill org=%s re_embedded=%d", org_id, count)
            total += count
        except Exception:
            _log.exception("embedding backfill failed for org=%s", org_id)
    return total


async def run_forever() -> None:
    interval = max(5, get_settings().embedding_migration_interval_seconds)
    # Fail loud, not silent: without the local-embedder extra in this
    # process image get_embedder().embed() raises and the LOCAL-tier
    # backfill is a permanent no-op -- every keyword-only blob stays
    # model_id='none' forever (the WS-A / 0a96ba96 failure mode). The
    # per-row except logs at debug only, so surface it once at startup.
    if not embedder_available():
        _log.warning(
            "embedding backfill: local embedder UNAVAILABLE in the worker process "
            "(no 'sentence-transformers' extra) -- the dense tier will NOT be "
            "backfilled and model_id='none' rows stay keyword-only. Build the "
            "worker image with the embedder extra (task WS-A / 0a96ba96)."
        )
    _log.info("embedding backfill worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
