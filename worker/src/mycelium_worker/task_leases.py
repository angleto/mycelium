"""Reclaim the task possessions of sessions that never came back.

The backstop that did not exist. Before migration 0017 an agent that
died mid-task left the task in a working state with nothing recording
that it had been taken and nothing to expire, so the task was stranded
permanently and only a human noticing it ever got it moving again. That
is the third of the three failures the lease exists for, and it is the
one that is a plain missing sweep rather than a missing concept.

**It releases the possession and does NOT move the task.** Where a task
should go back to is a workflow question with a workflow answer, and a
sweep guessing at it would write a transition nobody chose -- on a
workflow a project can override, so the guess would not even be stable
across projects. An unheld task sitting in a working state is visible in
every listing, pullable by anybody, and honest about what happened to it.
A task the sweep marched backwards is none of those, and it would also
destroy the evidence: the state it was in is what says how far the dead
session got.

Per-workspace and exception-isolated, the same shape as the other sweeps
here: enumerate orgs under ``admin_session``, then run each pass inside a
``tenant_session`` so RLS scopes the UPDATE.

A pass that reclaims a handful is the system working. A pass that
reclaims hundreds is a symptom -- either the TTL is far below how long
work actually takes here, or sessions are dying in numbers -- and the two
are told apart by whether the tasks come back quickly under a new holder.
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
from mycelium_core.services import task_leases

_log = logging.getLogger("mycelium.worker.task_leases")


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


async def run_once(batch_size: int | None = None) -> int:
    """One pass across all workspaces; returns how many were reclaimed."""
    limit = batch_size or get_settings().task_lease_sweep_batch
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("task-lease sweep: failed to list workspaces")
        return 0
    total = 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                task_ids = await task_leases.sweep_expired(s, limit=limit)
            if task_ids:
                # Logged with the task ids rather than only a count: the
                # question a human asks next is always "which ones", and
                # a count sends them to a query they would have to write.
                _log.info(
                    "task-lease sweep org=%s reclaimed=%d tasks=%s",
                    org_id,
                    len(task_ids),
                    ",".join(str(t) for t in task_ids[:20]),
                )
            total += len(task_ids)
        except Exception:
            _log.exception("task-lease sweep failed for org=%s", org_id)
    return total


async def run_forever() -> None:
    interval = max(5, get_settings().task_lease_sweep_interval_seconds)
    _log.info("task-lease sweep worker started (interval=%ds)", interval)
    while True:
        await asyncio.sleep(interval)
        await run_once()
