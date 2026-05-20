"""Telegram-aware notification sender (epic #125 P2).

Implements ``NotificationSender`` for the existing dispatch loop in
``services.notifications``. Two halves:

- **Outgoing send**: ``TelegramNotificationSender`` interprets the
  ``target`` field of ``NotificationPref`` as the Telegram ``chat_id``
  (BigInt, persisted as string in the existing schema). The actual
  HTTP call goes through the injectable ``TelegramApi`` Protocol so
  tests run against a recording fake. Other channels are delegated
  to a wrapped sender (an injected email/log/test sender), so the
  dispatcher pipeline stays a single seam rather than fanning into
  per-channel branches inside ``dispatch_pending``.
- **Pref auto-population on link**: ``sync_pref_from_link`` upserts
  ``NotificationPref(channel=telegram)`` with the linked chat_id as
  the target the moment the user redeems a code. The user can later
  toggle ``enabled`` from the SPA without re-doing the link dance.
  Mirrors how the existing ``set_pref`` API behaves but is invoked
  server-side at link time (the user does not have to manually copy
  their chat_id into a settings form: they could not even discover
  it from Telegram's UI). When the user unlinks, the pref is
  disabled (not deleted) so a relink restores it.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.notification import (
    NotificationChannelKind,
    NotificationPref,
)
from flow_core.notification_channel import NotificationSender
from flow_core.telegram_client import TelegramApi, get_telegram_api

logger = logging.getLogger("flow.telegram")


class TelegramNotificationSender:
    """Delegating sender: telegram goes through ``TelegramApi``, the
    rest go through the wrapped sender (typically an email sender or
    a no-op log sender for OSS deploys).

    The wrapped sender stays decoupled from Telegram knowledge, so the
    existing email path is unchanged."""

    def __init__(
        self,
        *,
        fallback: NotificationSender,
        api: TelegramApi | None = None,
    ) -> None:
        self._fallback = fallback
        self._api = api

    def _telegram(self) -> TelegramApi:
        return self._api if self._api is not None else get_telegram_api()

    async def send(
        self,
        *,
        channel: NotificationChannelKind,
        target: str,
        title: str,
        body: str,
    ) -> None:
        if channel is NotificationChannelKind.telegram:
            try:
                chat_id = int(target)
            except (TypeError, ValueError) as exc:
                # Per-item failure: the dispatcher catches and records
                # this on the notification row's ``last_error``.
                raise RuntimeError(f"invalid telegram target {target!r}: {exc}") from exc
            # Title + body on two lines is the conventional Telegram
            # layout (Markdown is not requested -- the body is
            # user-controlled and we deliberately do not let it
            # interpret formatting that could be confusing).
            text_payload = title if not body else f"{title}\n\n{body}"
            await self._telegram().send_message(chat_id=chat_id, text=text_payload)
            return
        await self._fallback.send(channel=channel, target=target, title=title, body=body)


async def sync_pref_from_link(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    chat_id: int,
) -> NotificationPref:
    """Upsert the user's telegram NotificationPref with the linked
    chat_id as the target. Called by the link service the moment a
    ``/start <code>`` succeeds. Idempotent: a second call with the
    same chat_id is a no-op; a re-link to a different chat updates
    the target and bumps the version."""
    pref = (
        await session.execute(
            select(NotificationPref).where(
                NotificationPref.user_id == user_id,
                NotificationPref.channel == NotificationChannelKind.telegram,
            )
        )
    ).scalar_one_or_none()
    target = str(chat_id)
    if pref is None:
        pref = NotificationPref(
            org_id=org_id,
            user_id=user_id,
            channel=NotificationChannelKind.telegram,
            enabled=True,
            target=target,
        )
        session.add(pref)
    elif pref.target != target or not pref.enabled:
        pref.target = target
        pref.enabled = True
        pref.version += 1
    await session.flush()
    return pref


async def disable_pref_on_unlink(
    session: AsyncSession, *, user_id: uuid.UUID
) -> None:
    """Soft-disable the user's telegram pref on unlink: clear the
    target so a future link refreshes it cleanly, but keep the row
    (a relink will toggle ``enabled`` back). Idempotent on a
    non-existent pref (an unlink without a prior link is a no-op)."""
    pref = (
        await session.execute(
            select(NotificationPref).where(
                NotificationPref.user_id == user_id,
                NotificationPref.channel == NotificationChannelKind.telegram,
            )
        )
    ).scalar_one_or_none()
    if pref is None:
        return
    if pref.enabled or pref.target:
        pref.enabled = False
        pref.target = ""
        pref.version += 1
        await session.flush()


__all__ = [
    "TelegramNotificationSender",
    "disable_pref_on_unlink",
    "sync_pref_from_link",
]
