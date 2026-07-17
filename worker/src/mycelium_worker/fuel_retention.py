"""Fuel-path retention sweep (ADR-0048, task 68052297).

Prunes the append-mostly telemetry tables ``retrieval_trace`` and
``search_clicks`` on fixed windows, per workspace, on a slow cadence
(daily by default). Registered UNCONDITIONALLY -- unlike the garden
sweep this is hygiene, not metabolism, so it must not depend on
``garden_loop_enabled`` (the historical hole: the only trace pruning
lived inside the edge-usage fold, which never runs in a stock deploy).

Per-workspace + exception-isolated, identical shape to
``revisions_retention``. The deletion logic lives in
``mycelium_core.services.fuel_retention`` (floored at the edge-usage
window, so the fold never loses rows it could still read).
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
from mycelium_core.services import fuel_retention as fuel_svc

_log = logging.getLogger("mycelium.worker.fuel_retention")


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


async def run_once() -> tuple[int, int]:
    """One sweep across all workspaces. Returns the cumulative counts
    ``(traces_deleted, clicks_deleted)`` for the worker log."""
    settings = get_settings()
    trace_days = settings.retrieval_trace_retention_days
    click_days = settings.search_click_retention_days
    total_traces = 0
    total_clicks = 0
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("fuel retention: failed to list workspaces")
        return (0, 0)
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                traces, clicks = await fuel_svc.prune(
                    s, trace_days=trace_days, click_days=click_days
                )
            total_traces += traces
            total_clicks += clicks
            if traces or clicks:
                _log.info(
                    "fuel retention org=%s traces=%d clicks=%d",
                    org_id,
                    traces,
                    clicks,
                )
        except Exception:
            _log.exception("fuel retention failed for org=%s", org_id)
    return (total_traces, total_clicks)


async def run_forever() -> None:
    """Periodic fuel-retention tick. Cadence comes from
    ``fuel_retention_interval_seconds`` (default 24h)."""
    interval = max(60, get_settings().fuel_retention_interval_seconds)
    _log.info("fuel retention worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
