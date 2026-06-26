"""Notification channel abstraction (FR-12).

One Protocol + injectable factory, the same seam pattern as the email
connector. v1 channels: Telegram and email (reference senders lazily
imported / SMTP-based; CI injects a recording fake). Per-user channel
prefs and dedupe are enforced in the service, not here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from mycelium_core.models.notification import NotificationChannelKind


@runtime_checkable
class NotificationSender(Protocol):
    async def send(
        self, *, channel: NotificationChannelKind, target: str, title: str, body: str
    ) -> None: ...


class DefaultSender:
    """Reference sender. Telegram via bot API / email via SMTP are
    wired at deploy time; not exercised in CI (the deterministic
    enqueue/dispatch/dedupe logic is)."""

    async def send(  # pragma: no cover - network
        self, *, channel: NotificationChannelKind, target: str, title: str, body: str
    ) -> None:
        raise RuntimeError("configure a concrete notification sender at deploy time")


_override: Callable[[], NotificationSender] | None = None


def set_sender_override(fn: Callable[[], NotificationSender] | None) -> None:
    """Test seam: inject a recording in-memory sender."""
    global _override
    _override = fn


def get_sender() -> NotificationSender:
    return _override() if _override is not None else DefaultSender()
