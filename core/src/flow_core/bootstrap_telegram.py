"""One-shot Telegram webhook bootstrap (epic #125 P2).

Calls ``setWebhook`` on the bot once at deploy time so Telegram
delivers updates to our public endpoint. Idempotent (Telegram's
setWebhook accepts the same URL repeatedly without error). Fails
closed: missing configuration aborts with a clear message rather
than registering a wrong URL.

Run: ``python -m flow_core.bootstrap_telegram`` from the backend
image at deploy time (same shape as ``bootstrap_admin``).

The webhook URL is composed as ``{telegram_webhook_base_url || frontend_base_url}
/api/telegram/webhook/{telegram_webhook_secret}``. The base URL must
be the public origin of the API (which is the SPA origin in the
co-host OSS deploy, hence the fallback to ``frontend_base_url``).
"""

from __future__ import annotations

import asyncio
import sys

from flow_core.config import get_settings
from flow_core.telegram_client import get_telegram_api


def _build_webhook_url(base: str, secret: str) -> str:
    """Compose the webhook URL. Tolerant of trailing slashes on the
    base; the secret is the path-segment auth factor (also re-asserted
    in the optional Telegram header)."""
    return f"{base.rstrip('/')}/api/telegram/webhook/{secret}"


async def run() -> str:
    settings = get_settings()
    if not settings.telegram_configured:
        raise SystemExit(
            "Telegram bot is not configured: FLOW_TELEGRAM_BOT_TOKEN, "
            "FLOW_TELEGRAM_BOT_USERNAME and FLOW_TELEGRAM_WEBHOOK_SECRET "
            "must all be set"
        )
    base = settings.telegram_webhook_url_base
    if not base:
        raise SystemExit(
            "Telegram webhook base URL is not set: provide "
            "FLOW_TELEGRAM_WEBHOOK_BASE_URL or FLOW_FRONTEND_BASE_URL"
        )
    url = _build_webhook_url(base, settings.telegram_webhook_secret)
    api = get_telegram_api()
    res = await api.set_webhook(url=url, secret_token=settings.telegram_webhook_secret)
    if not res.ok:
        raise SystemExit(f"setWebhook failed: {res.description}")
    return f"setWebhook ok: {url}"


def main() -> None:
    msg = asyncio.run(run())
    sys.stdout.write(msg + "\n")


if __name__ == "__main__":
    main()
