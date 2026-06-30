"""Periodic email connector sync worker (docs/adr/0023, FR-7).

The scheduled half of the F5 email connector: on a modest interval, every
workspace gets one ``sync_all_accounts`` tick (idempotent per
``(account, provider_message_id)``; per-account fault isolation lives in
the service). Without this job the connector is pull-only (the
``POST /email/accounts/{id}/sync`` route or the Email route's Sync
button); this closes the loop so work mail flows in on its own.

Session/context mirrors ``mycelium_worker.dispatch`` / ``mycelium_worker.reminders``
exactly: a worker has no API ``tenant_ctx``, so we enumerate workspaces
with the no-tenant ``admin_session`` (the system-actor enumeration window
opened by migration 0029 covers ``organizations`` + ``memberships`` and
ONLY those -- see test_worker_system_enumeration_rls). We resolve each
workspace owner deterministically and run the sweep inside a normal
``tenant_session(org, owner, actor_kind="system")``, where RLS is enforced
as for a human request, ``require_role(member)`` is satisfied by the
owner's stored membership, and the tenant-scoped read of ``email_accounts``
sees the workspace's accounts. We deliberately do NOT enumerate
``email_accounts`` under ``admin_session``: that table is outside the
system-enum window (and holds ``secret_encrypted``), so such a read would
return zero rows under FORCE RLS -- a silent no-op. A workspace with no
accounts is a cheap no-op (``sync_all_accounts`` lists none and returns).

Unlike the Google Calendar job this is NOT gated on
``settings.google_configured``: a workspace may hold only generic IMAP /
Proton accounts. Gmail accounts in a deploy without Google OAuth fail
per-account (``OAUTH_NOT_CONFIGURED``) inside ``sync_all_accounts`` and are
isolated there, never aborting the others.

Robustness: each workspace sweep is wrapped so one failing workspace never
stops the rest; the whole pass is wrapped so a transient DB error does not
kill the loop. Logged at info (one line per workspace that ingested mail).
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
from mycelium_core.services import email as email_svc

_log = logging.getLogger("mycelium.worker.email_sync")


async def _all_workspaces() -> list[uuid.UUID]:
    """Every workspace gets an email-sync tick (the presence of accounts is
    a tenant-scoped fact, resolved inside the per-org session; a workspace
    with none is a no-op). Deterministic order by ``str(id)``. Read with the
    no-tenant admin session: a global ``organizations`` scan, within the
    system-actor enumeration window."""
    async with admin_session() as s:
        orgs = (await s.execute(select(Organization).order_by(Organization.id))).scalars().all()
        return [o.id for o in sorted(orgs, key=lambda o: str(o.id))]


async def _owner_of(org_id: uuid.UUID) -> uuid.UUID | None:
    """The workspace owner the sweep acts as. Deterministic: the earliest
    owner membership by ``(created_at, str(user_id))``. ``None`` when the
    workspace has no owner (skip it -- nothing to act as). Mirrors
    ``mycelium_worker.dispatch._owner_of`` (same owner-authority convention)."""
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
    """One sweep: a ``sync_all_accounts`` tick per workspace. Returns the
    number of workspaces that ingested at least one new message (for logging
    / tests). Per-workspace exceptions are isolated and logged; the sweep
    itself is best-effort."""
    limit = max(1, get_settings().email_sync_fetch_limit)
    touched = 0
    try:
        org_ids = await _all_workspaces()
    except Exception:  # transient DB error: skip this sweep, keep looping
        _log.exception("email sync sweep: failed to list workspaces")
        return 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                results = await email_svc.sync_all_accounts(
                    s,
                    org_id=org_id,
                    actor_id=owner,
                    limit=limit,
                )
            created = sum(r.created for r in results)
            failed = [r for r in results if not r.ok]
            if created:
                _log.info(
                    "email sync org=%s accounts=%d created=%d failed=%d",
                    org_id,
                    len(results),
                    created,
                    len(failed),
                )
                touched += 1
            for r in failed:
                _log.warning(
                    "email sync org=%s account=%s failed: %s",
                    org_id,
                    r.account_id,
                    r.error,
                )
        except Exception:  # one workspace failing must not stop the rest
            _log.exception("email sync failed for org=%s", org_id)
    return touched


async def run_forever() -> None:
    """The periodic loop: ``run_once`` every
    ``settings.email_sync_interval_seconds``. Modest interval (do not hammer
    the IMAP/Gmail endpoints); never exits on a per-sweep error."""
    interval = max(30, get_settings().email_sync_interval_seconds)
    _log.info("email sync worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
