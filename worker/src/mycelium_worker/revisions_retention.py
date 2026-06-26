"""Retention + coarsening for the recovery-history table
(``entity_revision``).

Two passes per workspace, on a slow cadence (daily by default):

1. **Coarsen** sealed revisions older than
   ``revisions_retain_full_days``: keep one per (entity, day) up to
   ``revisions_coarse_to_weekly_days``, then one per (entity, week).
2. **Hard-delete** task / note rows whose ``deleted_at`` is older
   than ``revisions_hard_delete_after_days``. The AFTER DELETE
   cascade trigger then purges their revisions.

Per-workspace + exception-isolated, identical shape to
``task_search_backfill`` / ``garden`` / ``reminders``. The faster
``revisions.py`` worker handles the safety-net idle-seal on a
separate, sub-minute cadence — that one closes open web rows on
abandonment, this one prunes the long tail.
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
from mycelium_core.services import entity_revisions as revisions_svc

_log = logging.getLogger("mycelium.worker.revisions_retention")


async def _all_workspaces() -> list[uuid.UUID]:
    async with admin_session() as s:
        rows = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
        return [o.id for o in sorted(rows, key=lambda o: str(o.id))]


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
    return rows[0].user_id


async def run_once() -> tuple[int, int, int, int]:
    """One sweep across all workspaces. Returns the cumulative
    counts ``(daily_pruned, weekly_pruned, tasks_hard_deleted,
    notes_hard_deleted)`` so the worker log can summarise what
    happened."""
    settings = get_settings()
    full_days = settings.revisions_retain_full_days
    weekly_days = settings.revisions_coarse_to_weekly_days
    after_days = settings.revisions_hard_delete_after_days
    total_daily = 0
    total_weekly = 0
    total_tasks = 0
    total_notes = 0
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("revisions retention: failed to list workspaces")
        return (0, 0, 0, 0)
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                daily, weekly = await revisions_svc.coarsen(
                    s,
                    retain_full_days=full_days,
                    coarse_to_weekly_days=weekly_days,
                )
                tasks_d, notes_d = await revisions_svc.hard_delete_soft_deleted(
                    s, after_days=after_days
                )
            total_daily += daily
            total_weekly += weekly
            total_tasks += tasks_d
            total_notes += notes_d
            if daily or weekly or tasks_d or notes_d:
                _log.info(
                    "revisions retention org=%s daily=%d weekly=%d tasks_hard=%d notes_hard=%d",
                    org_id,
                    daily,
                    weekly,
                    tasks_d,
                    notes_d,
                )
        except Exception:
            _log.exception("revisions retention failed for org=%s", org_id)
    return (total_daily, total_weekly, total_tasks, total_notes)


async def run_forever() -> None:
    """Periodic retention/coarsening tick. Cadence comes from
    ``revisions_retention_interval_seconds`` (default 24h)."""
    interval = max(60, get_settings().revisions_retention_interval_seconds)
    _log.info(
        "revisions retention worker started (interval=%ds)",
        interval,
    )
    while True:
        await run_once()
        await asyncio.sleep(interval)
