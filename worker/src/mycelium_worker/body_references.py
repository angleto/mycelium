"""Written-reference drift repair (ADR-0073 decision 4).

The commit-time flush keeps ``body_reference`` in step with every body whose
write path marks it. This sweep is the instrument for the paths that do not:
per workspace, it re-projects every note part and task description whose
recorded digest differs from the live text (or that was never projected),
and logs how many it found. On a workspace that predates the projection the
first ticks are the backfill.

Registered UNCONDITIONALLY, like ``fuel_retention``: it calls no model and
spends no credits, and a projection left stale would show backlinks that are
not in the text. The count it repairs is the garden-health sensor
``reference_drift``; a repair on a steady workspace names a write path to
fix where it originates.
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
from mycelium_core.services import body_references as refs_svc

_log = logging.getLogger("mycelium.worker.body_references")

#: Bodies re-projected per transaction.
_TX_BODIES = 50


# The workspace enumeration every sweep in this package carries, same shape.
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
    return rows[0].user_id if rows else None


async def run_once() -> int:
    """One sweep across all workspaces. Returns the number of bodies
    re-projected."""
    batch = max(1, get_settings().body_reference_repair_batch)
    total = 0
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("reference repair: failed to list workspaces")
        return 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            parts = tasks = 0
            # Short transactions: each one locks the source rows it rewrites,
            # and a person editing one of those bodies waits for it.
            while parts + tasks < batch:
                async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                    report = await refs_svc.repair_drift(
                        s, org_id=org_id, limit=min(_TX_BODIES, batch - parts - tasks)
                    )
                parts += report.parts
                tasks += report.tasks
                if report.parts + report.tasks == 0:
                    break
            total += parts + tasks
            if parts or tasks:
                _log.info("reference repair org=%s parts=%d tasks=%d", org_id, parts, tasks)
        except Exception:
            _log.exception("reference repair failed for org=%s", org_id)
    return total


async def run_forever() -> None:
    """Periodic repair tick, every ``body_reference_repair_interval_seconds``."""
    interval = max(60, get_settings().body_reference_repair_interval_seconds)
    _log.info("reference repair worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
