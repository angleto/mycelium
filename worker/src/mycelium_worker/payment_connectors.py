"""Payment-connector processing worker (ADR-0051).

Drains ``payment_connector_events``: the inbound counterpart of the ADR-0047
delivery loop. Each tick sweeps every workspace and, per workspace:

  1. ``reclaim_expired`` returns events whose worker died to the pending pool,
     and ``purge_expired`` drops terminal rows past their retention window
     (never the parked ones -- those are the operator's queue);
  2. ``claim_due`` claims a bounded batch (``FOR UPDATE SKIP LOCKED``) and the
     claim is COMMITTED before any work starts;
  3. each claimed event is processed in its OWN transaction.

Steps 2 and 3 are separate transactions on purpose, and this is the one place
this loop deliberately differs from ``worker.webhooks``. The outbound deliverer
holds its transaction across the network call, so ``status='delivering'`` never
actually commits and a crash silently rewinds. Here the work is FISCAL: it
allocates a progressive number and files a document with SdI. If a crash could
rewind the claim, a second worker would pick the same event up and file it
twice. Committing the claim first makes the lease real -- and because
``payment_object_links`` is committed before the dispatch, the retry after an
expired lease RESUMES the existing document instead of composing a new one.

The lease (``payment_connector_lease_seconds``, 600) is longer than the whole
SdI dispatch budget (timeout 120 + dispatch lease 300), so an expired lease
provably means nothing is still in flight for that event.

Registered only when ``settings.payment_connectors_enabled``; a disabled deploy
never runs the loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select, text

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.organization import Organization
from mycelium_core.services import payment_connectors as svc

_log = logging.getLogger("mycelium.worker.payment_connectors")


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


async def _process_one(org_id: uuid.UUID, owner_id: uuid.UUID, event_id: uuid.UUID) -> str:
    """Run one event in its own transaction.

    The actor is the CONNECTOR, not the workspace owner: the audit trail must
    name what actually emitted the document. ``require_role`` inside the invoice
    service reads the pinned ``app.current_role`` GUC before falling back to a
    stored membership, which is how a non-user principal authorizes here (the
    same mechanism ``issuer_key_ctx`` uses on the public API).
    """
    async with tenant_session(
        str(org_id),
        str(owner_id),
        actor_kind="payment_connector",
        actor_subject_id=str(event_id),
    ) as session:
        await session.execute(
            text("SELECT set_config('app.current_role', :r, true)"),
            {"r": Role.member.value},
        )
        return await svc.process_event(session, org_id=org_id, event_id=event_id)


async def run_once() -> tuple[int, int]:
    """One sweep across all workspaces. Returns ``(processed, parked)``.

    ``parked`` counts events that ended in ``needs_attention`` or ``dead`` --
    the operator's queue. Per-workspace and per-event faults are isolated: one
    malformed payload must never stop the connector, and one broken workspace
    must never stop the sweep.
    """
    try:
        org_ids = await _all_workspaces()
    except Exception:
        _log.exception("payment connectors sweep: failed to list workspaces")
        return (0, 0)

    processed = 0
    parked = 0
    for org_id in org_ids:
        try:
            owner = await _owner_of(org_id)
            if owner is None:
                continue
            # Claim in its own transaction and COMMIT it before doing any work.
            async with tenant_session(str(org_id), str(owner), actor_kind="system") as s:
                await svc.reclaim_expired(s, org_id=org_id)
                await svc.purge_expired(s, org_id=org_id)
                claimed = await svc.claim_due(s, org_id=org_id)
            if not claimed:
                continue
            for event_id in claimed:
                try:
                    status = await _process_one(org_id, owner, event_id)
                    processed += 1
                    if status in {"needs_attention", "dead"}:
                        parked += 1
                        _log.warning(
                            "payment connector event parked org=%s event=%s status=%s",
                            org_id,
                            event_id,
                            status,
                        )
                except Exception:
                    # The event keeps its 'processing' lease and is reclaimed on
                    # a later tick; losing the row is not an option.
                    _log.exception(
                        "payment connector event failed org=%s event=%s", org_id, event_id
                    )
            _log.info(
                "payment connectors tick org=%s claimed=%d parked=%d",
                org_id,
                len(claimed),
                parked,
            )
        except Exception:
            _log.exception("payment connectors tick failed for org=%s", org_id)
    return (processed, parked)


async def run_forever() -> None:
    interval = max(5, get_settings().payment_connector_poll_interval_seconds)
    _log.info("payment connectors loop worker started (interval=%ds)", interval)
    while True:
        await run_once()
        await asyncio.sleep(interval)
