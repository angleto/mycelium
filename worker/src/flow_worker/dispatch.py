"""Closed-loop dispatch worker job (docs/adr/0025, P5).

The periodic half of the closed loop: on a modest interval, for every
workspace whose ``autonomous_dispatch`` policy is not ``off``, run ONE
``dispatch_loop.tick`` (recompute -> admit -> governance gate ->
dispatch via the P3 metered path). The loop itself (the
at-most-one-active-request invariant, the human-in-the-loop default,
the per-tick churn cap, the non-fatal per-task isolation) lives in
``flow_core.services.dispatch_loop``; this module only schedules it and
isolates failures per workspace.

Session/context: the loop runs **as the workspace owner's authority**
(dispatch is owner-gated). A worker does not pass through the API
``tenant_ctx`` so no ``app.current_role`` GUC is set; ``rbac.require_role``
then falls back to the actor's stored membership (its documented
worker/test path). We therefore enumerate workspaces with the no-tenant
``admin_session`` (a global ``organizations`` read), resolve the
workspace owner deterministically, and run the tick inside a normal
``tenant_session(org, owner)`` so RLS is enforced exactly as for a human
request and the owner's stored membership satisfies the owner gate.
This reuses the existing session helpers -- no fabricated auth path.

Robustness: each workspace tick is wrapped so one failing workspace
never stops the others; the whole sweep is wrapped so a transient DB
error does not kill the loop. Everything is logged at info (a one-line
summary per workspace that did work).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.models.dispatch_request import AutonomousDispatch
from flow_core.models.membership import Membership, Role
from flow_core.models.organization import Organization
from flow_core.services import dispatch_loop
from flow_core.services.dispatch_loop import resolve_policy

_log = logging.getLogger("flow.worker.dispatch")


async def _enabled_workspaces() -> list[uuid.UUID]:
    """Workspace ids whose policy is not ``off`` (the governance-default
    ``approval_required`` IS enabled -- the loop still creates pending
    requests there; only an explicit ``off`` disables it). Deterministic
    order by ``str(id)``. Read with the no-tenant admin session: it is a
    global ``organizations`` scan, not a per-tenant query."""
    async with admin_session() as s:
        orgs = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
        return [
            o.id
            for o in sorted(orgs, key=lambda o: str(o.id))
            if resolve_policy(o) is not AutonomousDispatch.off
        ]


async def _owner_of(org_id: uuid.UUID) -> uuid.UUID | None:
    """The workspace owner the loop acts as. Deterministic: the earliest
    owner membership by ``(created_at, str(user_id))``. ``None`` if the
    workspace has no owner (skip it -- nothing to act as)."""
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


async def run_once() -> int:
    """Run one sweep: a tick per enabled workspace. Returns the number
    of workspaces that dispatched at least one run (for logging/tests).
    Per-workspace exceptions are isolated and logged; the sweep itself
    is best-effort."""
    dispatched_workspaces = 0
    try:
        org_ids = await _enabled_workspaces()
    except Exception:  # transient DB error: skip this sweep, keep looping
        _log.exception("dispatch sweep: failed to list workspaces")
        return 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner)) as s:
                res = await dispatch_loop.tick(s, org_id=org_id, actor_id=owner)
            if res.created or res.dispatched or res.skipped or res.failed:
                _log.info(
                    "dispatch tick org=%s policy=%s created=%d dispatched=%d "
                    "skipped=%d failed=%d makespan_min=%d cost=%s",
                    org_id,
                    res.policy.value,
                    res.created,
                    res.dispatched,
                    res.skipped,
                    res.failed,
                    res.projected_makespan_minutes,
                    res.projected_credit_cost,
                )
            if res.dispatched:
                dispatched_workspaces += 1
        except Exception:  # one workspace failing must not stop the rest
            _log.exception("dispatch tick failed for org=%s", org_id)
    return dispatched_workspaces


async def run_forever() -> None:
    """The periodic loop: ``run_once`` every
    ``settings.dispatch_loop_interval_seconds``. Modest interval (do not
    hammer the scheduler); never exits on a per-sweep error."""
    interval = max(5, get_settings().dispatch_loop_interval_seconds)
    _log.info("dispatch loop worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
