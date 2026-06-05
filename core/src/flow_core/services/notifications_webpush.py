"""Web Push ``NotificationSender`` (channel=webpush).

Wraps ``pywebpush`` (sync, requests-based) in ``asyncio.to_thread`` like
the SMTP mailer. The ``target`` is a JSON-encoded SINGLE subscription
(``{"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}}``); the
dispatcher fans out, calling :meth:`send` once per subscription. Any other
channel is delegated to the wrapped fallback sender, so this composes with
the telegram/email senders as one seam.

A 404/410 from the push service means the subscription is permanently gone
-> raise :class:`WebPushGone` so the dispatcher prunes that row.
"""

from __future__ import annotations

import asyncio
import json

from flow_core.config import Settings, get_settings
from flow_core.models.notification import NotificationChannelKind
from flow_core.notification_channel import NotificationSender


class WebPushGone(Exception):
    """The push endpoint is permanently gone (404/410); prune the row."""


class WebPushNotificationSender:
    """Delegating sender: webpush goes through ``pywebpush``, everything
    else through the wrapped fallback (telegram-over-email)."""

    def __init__(self, *, fallback: NotificationSender) -> None:
        self._fallback = fallback

    async def send(
        self,
        *,
        channel: NotificationChannelKind,
        target: str,
        title: str,
        body: str,
    ) -> None:
        if channel is not NotificationChannelKind.webpush:
            await self._fallback.send(channel=channel, target=target, title=title, body=body)
            return
        settings = get_settings()
        if not settings.vapid_configured:
            # Per-item failure recorded by the dispatcher; safe default when
            # VAPID is not set up on this deploy.
            raise RuntimeError("web push is not configured")
        subscription = json.loads(target)
        payload = json.dumps({"title": title, "body": body})
        await asyncio.to_thread(self._send_sync, subscription, payload, settings)

    @staticmethod
    def _send_sync(
        subscription: dict[str, object], payload: str, settings: Settings
    ) -> None:  # pragma: no cover - network
        from pywebpush import WebPushException, webpush

        try:
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                raise WebPushGone(str(exc)) from exc
            raise
