"""Telegram bot integration router (epic #125 P2).

Four endpoints:

- ``POST /telegram/link/request`` (authenticated): mint a single-use
  deep-link code; the SPA renders the resulting ``t.me`` URL / QR.
- ``GET /telegram/link/status`` (authenticated): read the caller's
  current link (linked / chat_username / linked_at).
- ``DELETE /telegram/link`` (authenticated): unlink. Also disables
  the caller's telegram NotificationPref so the dispatcher does not
  keep trying to deliver to a no-longer-bound chat.
- ``POST /telegram/webhook/{secret}`` (public): Telegram's bot
  webhook. Authentication is two-factor: the path secret AND the
  optional ``X-Telegram-Bot-Api-Secret-Token`` header (Telegram's
  standard mechanism, registered via ``setWebhook``). Mismatch on
  either: 403. Idempotent by Telegram's ``update_id``.

The bot's webhook reply (``sendMessage`` back to the user) is a
side-effect, not the HTTP response. We respond 200 quickly so
Telegram does not retry; the reply, if any, is dispatched in the
background via the injected ``TelegramApi``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    TelegramLinkRequestOut,
    TelegramLinkStatusOut,
    TelegramWebhookOut,
)
from flow_core.config import get_settings
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.services import telegram_link as svc
from flow_core.services.notifications_telegram import disable_pref_on_unlink
from flow_core.telegram_client import get_telegram_api

logger = logging.getLogger("flow.telegram")

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/link/request", response_model=TelegramLinkRequestOut)
async def request_link(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> TelegramLinkRequestOut:
    settings = get_settings()
    if not settings.telegram_configured:
        raise DomainError(MessageCode.TELEGRAM_NOT_CONFIGURED)
    issued = await svc.create_link_code(
        ctx.session,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        bot_username=settings.telegram_bot_username,
    )
    return TelegramLinkRequestOut(
        code=issued.code,
        expires_at=issued.expires_at,
        bot_username=issued.bot_username,
        deep_link=issued.deep_link,
    )


@router.get("/link/status", response_model=TelegramLinkStatusOut)
async def get_status(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> TelegramLinkStatusOut:
    s = await svc.get_link_status(ctx.session, user_id=ctx.user_id)
    return TelegramLinkStatusOut(
        linked=s.linked, chat_username=s.chat_username, linked_at=s.linked_at
    )


@router.delete("/link", status_code=status.HTTP_204_NO_CONTENT)
async def unlink(ctx: Annotated[TenantCtx, Depends(tenant_ctx)]) -> None:
    await svc.unlink(ctx.session, user_id=ctx.user_id)
    await disable_pref_on_unlink(ctx.session, user_id=ctx.user_id)


@router.post(
    "/webhook/{secret}",
    response_model=TelegramWebhookOut,
    include_in_schema=False,
)
async def webhook(
    request: Request,
    secret: str,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> TelegramWebhookOut:
    """Telegram webhook. Telegram retries a non-2xx delivery
    aggressively, so we always answer 200 unless the request itself
    fails auth (then 403, so Telegram's "test webhook" surfaces the
    error). The HTTP response is *not* the user-facing reply; the
    reply is sent via ``TelegramApi.send_message`` as a side-effect."""
    settings = get_settings()
    if not settings.telegram_configured:
        # Webhook is disarmed: 404 hides the existence of the endpoint
        # to scanners and signals "this deploy did not configure the
        # bot" to a benign caller. We do not raise DomainError because
        # the path was not reached via the i18n surface; this is the
        # unauthenticated edge.
        raise HTTPException(status_code=404, detail="not found")
    expected_secret = settings.telegram_webhook_secret
    if not expected_secret or secret != expected_secret:
        raise HTTPException(status_code=403, detail="forbidden")
    # Telegram's optional header re-asserts the secret. When the deploy
    # registered the webhook with ``secret_token``, every legitimate
    # update carries it. If the deploy did NOT register one, the
    # header is absent and we accept (the path-secret already auth'd).
    if (
        x_telegram_bot_api_secret_token is not None
        and x_telegram_bot_api_secret_token != expected_secret
    ):
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        # Malformed JSON: Telegram never sends this; treat as 400 so
        # any downstream proxy sees the error rather than retry.
        raise HTTPException(status_code=400, detail="invalid payload") from None

    outcome = await svc.handle_webhook_update(payload)
    if outcome.reply_text and isinstance(payload.get("message"), dict):
        chat = payload["message"].get("chat") or {}
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if isinstance(chat_id, int):
            try:
                await get_telegram_api().send_message(chat_id=chat_id, text=outcome.reply_text)
            except Exception:
                # Reply is best-effort; never fail the webhook on a
                # transient Telegram error (Telegram would retry).
                logger.exception("telegram reply failed for chat_id=%s", chat_id)
    return TelegramWebhookOut(ok=True)


__all__ = ["router"]
