"""Concrete ``NotificationSender`` wiring (FR-12).

``notification_channel.get_sender`` defaults to ``DefaultSender``, which
*raises* on every send: a deploy MUST install a concrete sender, exactly
like the system mailer (``build_system_mailer`` + ``set_mailer``). Until
this module is wired, every reminder/notification dispatch fails with
``configure a concrete notification sender at deploy time`` and the
notification row is marked ``failed`` -- so reminders never arrive.

This factory mirrors ``build_system_mailer``:

  * the **email** channel is delegated to the already-wired system mailer
    (``LogMailer`` in dev/OSS, ``SmtpMailer`` in prod), so email
    notifications reuse the same transport as verification/reset mail;
  * the **telegram** channel goes through ``TelegramNotificationSender``,
    which uses the global ``get_telegram_api()`` (the configured Bot API,
    or a fail-closed stub that records a per-item failure when the bot is
    not configured).

Wired once at startup in ``flow_api.app`` (lifespan) and
``flow_worker.main`` via ``set_sender_override`` -- the same single-seam
pattern as ``set_mailer``. The ASGI test transport does not run the
lifespan and ``flow_worker.main`` is never imported by the test suite, so
the recording fakes that tests inject via ``set_sender_override`` are
never clobbered.
"""

from __future__ import annotations

from flow_core.models.notification import NotificationChannelKind
from flow_core.notification_channel import NotificationSender
from flow_core.services.mailer import OutboundEmail, get_mailer
from flow_core.services.notifications_telegram import TelegramNotificationSender
from flow_core.services.notifications_webpush import WebPushNotificationSender


class EmailNotificationSender:
    """Adapt the system mailer to the ``NotificationSender`` seam.

    Handles only the email channel (``target`` is the recipient address,
    ``title`` the subject, ``body`` the text). Any other channel is a
    per-item failure recorded by the dispatcher on the notification row's
    ``last_error`` -- the safe outcome when, e.g., a user has a telegram
    pref but the bot is not configured on this deploy."""

    async def send(
        self, *, channel: NotificationChannelKind, target: str, title: str, body: str
    ) -> None:
        if channel is not NotificationChannelKind.email:
            raise RuntimeError(f"no sender configured for channel {channel.value}")
        await get_mailer().send(OutboundEmail(to=target, subject=title, body=body))


def build_notification_sender() -> NotificationSender:
    """Web Push -> Telegram -> email delegating chain.

    Each sender handles its own channel and delegates the rest to its
    fallback, so the dispatcher stays a single seam. Reads no settings
    eagerly: webpush resolves the VAPID config at send time, the telegram
    half resolves the live ``get_telegram_api()`` (configured Bot API or
    fail-closed stub), and the email half resolves the live
    ``get_mailer()``. All are wired earlier in the same startup path, so
    this only needs calling once after ``set_mailer``."""
    return WebPushNotificationSender(
        fallback=TelegramNotificationSender(fallback=EmailNotificationSender())
    )
