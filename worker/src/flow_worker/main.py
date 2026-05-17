"""Worker skeleton.

Background jobs (IMAP sync, scheduler, memory promotion/re-embedding,
recurrences, reminders, SdI receipts) land with their phases. F0 ships
only a runnable entry point that wires logging and validates config.
"""

from __future__ import annotations

import logging

from flow_core.config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logging.getLogger("flow.worker").info(
        "worker skeleton started (env=%s); no jobs registered yet",
        settings.env,
    )


if __name__ == "__main__":
    main()
