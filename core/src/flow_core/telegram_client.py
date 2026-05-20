"""Telegram Bot API client + test seam (epic #125 P2).

One Protocol + injectable factory, the same seam pattern as the
``NotificationSender`` and ``EmailConnector``. The Protocol lets tests
inject a recording fake without monkey-patching, and lets the rest of
the code (the bot router, the Telegram NotificationSender, the
``bootstrap_telegram`` CLI) stay decoupled from ``httpx`` and from
api.telegram.org. Tests run with ``set_telegram_api_override`` and
exercise the real domain logic against a fake.

The concrete ``HttpxTelegramApi`` is the production implementation;
it speaks the Telegram Bot HTTP API directly (no python-telegram-bot
dependency: that library bundles its own dispatcher / scheduler and
is overkill for the two endpoints we use). The bot token is *not*
persisted: it lives in ``Settings`` (env / k8s Secret).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from flow_core.config import get_settings

logger = logging.getLogger("flow.telegram")


@dataclass(frozen=True, slots=True)
class TelegramSendResult:
    """The Telegram-side message id of a delivered ``sendMessage`` (the
    int Telegram assigns; useful for threading / future edits)."""

    message_id: int


@dataclass(frozen=True, slots=True)
class TelegramSetWebhookResult:
    ok: bool
    description: str


@runtime_checkable
class TelegramApi(Protocol):
    """Two-call surface: send a message to a chat, register the webhook.

    The Protocol stays small on purpose: every other Telegram feature
    we might want later (sendPhoto, editMessage, ...) goes through the
    same seam, but for P2 these are the only two operations the
    backend needs at runtime."""

    async def send_message(self, *, chat_id: int, text: str) -> TelegramSendResult: ...

    async def set_webhook(self, *, url: str, secret_token: str) -> TelegramSetWebhookResult: ...


class HttpxTelegramApi:
    """Production implementation. The bot token is the only secret in
    play and is sourced from Settings at construction time, never
    persisted to the DB (the bot is per-deploy, not per-tenant).

    Network errors surface as ``httpx.HTTPError``; the caller (the
    NotificationSender or the webhook reply path) is responsible for
    isolating per-item failures, matching the existing notification
    dispatch contract (one failure never aborts the batch)."""

    def __init__(self, *, bot_token: str, timeout_seconds: float) -> None:
        if not bot_token:
            # Fail-closed: the seam (the Protocol) is always available,
            # but instantiating the *real* impl with no token is a
            # configuration error -- not something to silently no-op
            # (a no-op would mask deploy mistakes; ``telegram_configured``
            # gates construction at the call sites).
            raise ValueError("HttpxTelegramApi requires a non-empty bot token")
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._timeout = timeout_seconds

    async def send_message(self, *, chat_id: int, text: str) -> TelegramSendResult:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            # Telegram returns 200 + {"ok": false} on application errors
            # (blocked by user, chat not found, ...); surface as the
            # same exception shape as transport errors so the sender
            # records a unified failure.
            raise httpx.HTTPError(
                f"telegram sendMessage rejected: {payload.get('description', 'unknown')}"
            )
        result = payload.get("result") or {}
        message_id = int(result.get("message_id") or 0)
        return TelegramSendResult(message_id=message_id)

    async def set_webhook(self, *, url: str, secret_token: str) -> TelegramSetWebhookResult:
        body: dict[str, str] = {"url": url}
        if secret_token:
            # Telegram echoes this back as the
            # ``X-Telegram-Bot-Api-Secret-Token`` header on every
            # webhook delivery -- the webhook handler asserts it.
            body["secret_token"] = secret_token
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._base}/setWebhook", json=body)
                payload = resp.json()
            return TelegramSetWebhookResult(
                ok=bool(payload.get("ok")),
                description=str(payload.get("description", "")),
            )
        except httpx.HTTPError as exc:
            return TelegramSetWebhookResult(ok=False, description=str(exc))


class _UnconfiguredTelegramApi:
    """Fallback when no bot token is configured. Refuses to talk; the
    Telegram NotificationSender records a per-item failure instead of
    raising. Same role as the ``DefaultSender`` for notifications: a
    safe-by-construction default for OSS deploys that have not set up
    the integration yet."""

    async def send_message(self, *, chat_id: int, text: str) -> TelegramSendResult:
        raise RuntimeError("telegram bot is not configured")

    async def set_webhook(self, *, url: str, secret_token: str) -> TelegramSetWebhookResult:
        return TelegramSetWebhookResult(ok=False, description="telegram bot is not configured")


_override: Callable[[], TelegramApi] | None = None


def set_telegram_api_override(fn: Callable[[], TelegramApi] | None) -> None:
    """Test seam: inject a recording in-memory ``TelegramApi``."""
    global _override
    _override = fn


def get_telegram_api() -> TelegramApi:
    """Return the active ``TelegramApi``. Order: test override >
    configured ``HttpxTelegramApi`` (when ``telegram_configured``) >
    ``_UnconfiguredTelegramApi`` fallback."""
    if _override is not None:
        return _override()
    settings = get_settings()
    if settings.telegram_configured:
        return HttpxTelegramApi(
            bot_token=settings.telegram_bot_token,
            timeout_seconds=settings.telegram_http_timeout_seconds,
        )
    return _UnconfiguredTelegramApi()


def telegram_deep_link(*, bot_username: str, code: str) -> str:
    """Build the ``https://t.me/<bot>?start=<code>`` deep link the SPA
    surfaces in the "Link Telegram" modal. Strips a leading ``@`` from
    the username (the env var commonly carries it; the URL does not)."""
    handle = bot_username.lstrip("@")
    return f"https://t.me/{handle}?start={code}"


__all__ = [
    "HttpxTelegramApi",
    "TelegramApi",
    "TelegramSendResult",
    "TelegramSetWebhookResult",
    "get_telegram_api",
    "set_telegram_api_override",
    "telegram_deep_link",
]
