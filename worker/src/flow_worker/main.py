"""Worker entry point.

Background jobs (IMAP sync, scheduler, memory promotion/re-embedding,
recurrences, reminders, SdI receipts) land with their phases. The first
registered job is the ADR-0025 P5 closed-loop dispatch tick: on a modest
interval it runs ``dispatch_loop.tick`` per workspace whose autonomous
policy is not ``off`` (recompute -> admit -> governance gate -> dispatch
via the P3 metered path). Per-workspace exception-isolated, logged at
info; the loop logic itself lives in ``flow_core.services.dispatch_loop``.
"""

from __future__ import annotations

import asyncio
import logging

from flow_core.config import get_settings
from flow_worker import dispatch


async def _run() -> None:
    # Registered jobs run concurrently; today only the P5 dispatch loop.
    await asyncio.gather(dispatch.run_forever())


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logging.getLogger("flow.worker").info(
        "worker started (env=%s); jobs: dispatch-loop",
        settings.env,
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
