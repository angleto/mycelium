"""Safety-net seal for the recovery-history web channel.

The SPA explicitly seals an editing session on blur / route change
/ Cmd+S via ``POST /tasks|notes/{id}/edit-session/seal``. Anything
the SPA fails to close (tab killed, network drop, hard reload) is
caught here: every tick this job seals any ``web``-channel revision
whose ``last_edit_at`` is older than
``IDLE_SAFETY_SEAL_SECONDS`` (60s). Idempotent.

``entity_revision`` is FORCE-RLS, so the sweep runs per workspace
via ``tenant_session`` (same shape as ``task_search_backfill`` /
``garden`` / ``reminders``). Per-tenant exception isolation: a
single workspace failure does not stop the loop.

Coarsening / retention (1 rev/day after 30 days, hard-delete on
soft-deleted entities after 90 days) is a separate concern that
lands on a slower cadence; tracked separately.
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

_log = logging.getLogger("mycelium.worker.revisions")

# Tick faster than the safety-net cutoff so an abandoned window
# closes within roughly one extra cutoff. The cutoff is 60s; a 30s
# tick lands a seal within 90s worst-case, which is good enough
# for a recovery-history UX.
_SEAL_TICK_SECONDS = 30


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


async def run_once() -> int:
    """One sweep across all workspaces; per-tenant exception
    isolation. Returns the cumulative number of revisions sealed
    across orgs."""
    total = 0
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("revisions idle-seal: failed to list workspaces")
        return 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                sealed = await revisions_svc.seal_idle(s)
            if sealed:
                _log.info(
                    "entity_revision idle-seal org=%s closed %d open web revisions",
                    org_id,
                    sealed,
                )
                total += sealed
        except Exception:
            _log.exception("entity_revision idle-seal failed for org=%s", org_id)
    return total


async def run_forever() -> None:
    """Periodic safety-net seal. Cadence is hardcoded at 30s: per-org
    cost is a single UPDATE against a partial index, so even a few
    hundred workspaces stay well under the tick budget. Survives
    transient DB errors via ``run_once``'s exception isolation."""
    get_settings()  # share the rest of the worker's settings plumbing
    _log.info(
        "revisions safety-net seal worker started (tick=%ds)",
        _SEAL_TICK_SECONDS,
    )
    while True:
        await run_once()
        await asyncio.sleep(_SEAL_TICK_SECONDS)
