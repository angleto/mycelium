"""Periodic half of the Telegram conversational assistant (ADR-0026, P3).

On a short interval, drain the assistant job queue: for each pending job
run the LLM turn (with the chat's recent history) and send the reply.
The turn logic lives in ``flow_core.services.assistant``; this module
only schedules it and isolates per-tick failures (one bad tick never
kills the loop). No-op when the assistant is disabled.
"""

from __future__ import annotations

import asyncio
import logging

from flow_core.config import get_settings
from flow_core.services import assistant as assistant_svc

_log = logging.getLogger("flow.worker.assistant")


async def run_once() -> int:
    """One drain pass: returns the number of jobs processed."""
    return await assistant_svc.process_pending_jobs(limit=10)


async def run_forever() -> None:
    settings = get_settings()
    if not settings.assistant_enabled:
        _log.info("telegram assistant worker disabled (FLOW_ASSISTANT_ENABLED=false)")
        return
    interval = max(2, settings.assistant_loop_interval_seconds)
    _log.info("telegram assistant worker started (interval=%ds)", interval)
    while True:
        try:
            processed = await run_once()
            if processed:
                _log.info("assistant: processed %d job(s)", processed)
        except Exception:
            _log.exception("assistant loop tick failed")
        await asyncio.sleep(interval)
