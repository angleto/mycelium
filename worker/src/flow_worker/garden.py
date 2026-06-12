"""Garden seasonal-rule worker (docs/adr/0029 P1).

Periodic sweep that applies the automatic maturity transitions on
notes:

- ``seed`` -> ``growing`` when touched within ``seed_to_growing_days``
- ``growing`` / ``mature`` -> ``dormant`` when untouched for
  ``growing_to_dormant_days``
- ``dormant`` -> ``growing`` when touched again

Manual transitions (`services/note_links.set_maturity`) always win
over the worker. ``mature`` is never set automatically (the user
decides).

Each tick runs per workspace as the workspace owner, mirroring the
dispatch worker pattern (own session, own RLS, own audit). Failures
on a single workspace are isolated; the loop keeps going.
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
from flow_core.services import garden_classify, garden_health, graph_snapshot, note_links

_log = logging.getLogger("flow.worker.garden")


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
    """One sweep over all workspaces. Returns the total number of
    notes whose maturity changed in this tick (across all
    workspaces)."""
    total = 0
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("garden sweep: failed to list workspaces")
        return 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                counters = await note_links.tick_maturity_transitions(
                    s, org_id=org_id, actor_id=owner
                )
                # Value-axis auto-promotion growing -> mature (ADR-0032).
                # Reversible (label-only, audited + feedback event); the
                # global flag disables it outright.
                auto_matured = 0
                if get_settings().garden_auto_mature_enabled:
                    auto_matured = await garden_classify.auto_promote_mature(
                        s, org_id=org_id, actor_id=owner
                    )
            # Garden-health daily snapshot (ADR-0035), in its OWN session
            # so a sensor-query failure can never roll back the maturity
            # transitions committed above. Idempotent per (org, day).
            try:
                async with tenant_session(str(org_id), str(owner), actor_kind="system") as hs:
                    await garden_health.persist_snapshot(hs, org_id=org_id)
            except Exception:
                _log.exception("garden health snapshot failed for org=%s", org_id)
            # Graph-analytics materialisation (task d8664631): PageRank +
            # Leiden + betweenness into garden_graph_snapshot. Signature-
            # gated, so an unchanged graph costs three COUNT queries; only
            # a real change pays the O(V·E) betweenness. Own session for
            # the same failure-isolation reason as the health snapshot.
            try:
                async with tenant_session(str(org_id), str(owner), actor_kind="system") as gs:
                    await graph_snapshot.refresh_graph_snapshot(gs, org_id=org_id)
            except Exception:
                _log.exception("graph snapshot refresh failed for org=%s", org_id)
            n = sum(counters.values()) + auto_matured
            if n > 0:
                _log.info(
                    "garden tick org=%s seed_to_growing=%d to_dormant=%d "
                    "dormant_to_growing=%d auto_matured=%d",
                    org_id,
                    counters["seed_to_growing"],
                    counters["to_dormant"],
                    counters["dormant_to_growing"],
                    auto_matured,
                )
                total += n
        except Exception:
            _log.exception("garden tick failed for org=%s", org_id)
    return total


async def run_forever() -> None:
    """Periodic loop: ``run_once`` on a modest interval. Reuses the
    ``dispatch_loop_interval_seconds`` setting as a sensible default;
    can diverge in the future via a dedicated setting."""
    interval = max(60, get_settings().dispatch_loop_interval_seconds * 4)
    _log.info("garden loop worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
