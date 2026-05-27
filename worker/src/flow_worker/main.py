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

from flow_core.ai_providers import set_llm_override
from flow_core.config import get_settings
from flow_core.llm_ollama import OllamaLLM
from flow_core.services.mailer import build_system_mailer, set_mailer
from flow_worker import (
    dispatch,
    embedding_migration,
    google_calendar,
    reminders,
    revisions,
    revisions_retention,
    revisions_summary,
    task_search_backfill,
    telegram_assistant,
)


async def _run() -> None:
    # Registered jobs run concurrently:
    #  - P5 closed-loop dispatch tick;
    #  - epic #125 P1 Google Calendar periodic ingest (no-op when
    #    Google OAuth is not configured);
    #  - ADR-0026 P3 Telegram assistant queue drain (no-op when
    #    FLOW_ASSISTANT_ENABLED is false);
    #  - reminders + notification-dispatch tick (per-workspace
    #    scan_reminders + dispatch_pending; closes the FR-12 loop);
    #  - task-search embedding backfill (re-embeds task blobs whose
    #    initial write timed out; the listener-driven resync is the
    #    authoritative path, this is a safety net).
    await asyncio.gather(
        dispatch.run_forever(),
        google_calendar.run_forever(),
        telegram_assistant.run_forever(),
        reminders.run_forever(),
        task_search_backfill.run_forever(),
        revisions.run_forever(),
        revisions_retention.run_forever(),
        revisions_summary.run_forever(),
        embedding_migration.run_forever(),
    )


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    # Process-global mailer wiring for the real worker process (mirrors
    # the API lifespan). Only swap when SMTP is configured; unconfigured
    # the module default LogMailer stands. main() is the process entry,
    # never imported by the test-suite, so no test mailer is clobbered.
    if settings.smtp_configured:
        set_mailer(build_system_mailer(settings))
    # Wire the open-model LLM (Ollama) when configured. Without this
    # the ``LocalLLM`` stub stays in place and the revision-summary
    # sweep is a no-op -- CI, dev and unconfigured deploys never hit
    # the network. Build a frozen URL+model snapshot so the override
    # is callable (signature ``() -> LLMProvider``).
    if settings.ollama_url and settings.open_model:
        ollama_url = settings.ollama_url
        open_model = settings.open_model
        set_llm_override(lambda: OllamaLLM(base_url=ollama_url, model=open_model))
    logging.getLogger("flow.worker").info(
        "worker started (env=%s); jobs: dispatch-loop, google-calendar-sync, telegram-assistant",
        settings.env,
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
