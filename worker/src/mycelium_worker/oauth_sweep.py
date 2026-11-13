"""Remove OAuth rows nobody completed (ADR-0069).

Dynamic Client Registration and ``/authorize`` both write a row for a
caller who holds nothing, which is the point of them and also why their
rows must not accumulate. The API bounds how fast one network origin can
write them; this sweep is what makes them go away:

- dynamic client registrations that never completed a consent, a day after
  they were made (``oauth_clients.sweep_unused``). A host registers and
  sends the person to the consent page within seconds, so a day-old unused
  registration is abandoned. Hand-registered clients and clients that have
  completed a consent are never touched.
- authorization requests a week past their expiry
  (``oauth_consent.sweep_expired``), kept that long so an approval can be
  traced to the assistant it produced while anybody might still ask.

Pre-tenant tables, so one pass in the system session with no workspace --
the only session the row policy on ``oauth_clients`` lets see the dynamic
rows -- rather than the per-workspace fan-out the other sweeps use. The
two deletes are separate transactions so a failure in one does not stop
the other.
"""

from __future__ import annotations

import asyncio
import logging

from mycelium_core.db import admin_session
from mycelium_core.services import oauth_clients, oauth_consent

_log = logging.getLogger("mycelium.worker.oauth_sweep")

# Hourly. The windows it enforces are a day and a week, so a finer cadence
# would only add queries; a coarser one would let an abandoned registration
# outlive its day by most of another.
INTERVAL_SECONDS = 3600


async def run_once() -> tuple[int, int]:
    """One pass. Returns ``(clients_deleted, requests_deleted)``."""
    clients = 0
    requests = 0
    try:
        async with admin_session() as s:
            clients = await oauth_clients.sweep_unused(s)
    except Exception:
        _log.exception("oauth sweep: unused client registrations failed")
    try:
        async with admin_session() as s:
            requests = await oauth_consent.sweep_expired(s)
    except Exception:
        _log.exception("oauth sweep: expired authorization requests failed")
    if clients or requests:
        _log.info("oauth sweep clients=%d requests=%d", clients, requests)
    return clients, requests


async def run_forever() -> None:
    _log.info("oauth sweep worker started (interval=%ds)", INTERVAL_SECONDS)
    while True:
        await run_once()
        await asyncio.sleep(INTERVAL_SECONDS)


__all__ = ["INTERVAL_SECONDS", "run_forever", "run_once"]
