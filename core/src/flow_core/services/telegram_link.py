"""Telegram link service (epic #125 P2).

Two responsibilities, isolated from the HTTP adapter:

- **Authenticated link minting**: the SPA's "Link Telegram" button.
  Mints a fresh ``TelegramLinkCode`` (single-use, 15 min TTL) for the
  caller, invalidating any unconsumed code already in flight. Returns
  the code + the deep-link URL the user opens in Telegram.
- **Webhook redemption + ingestion**: the bot HTTP webhook. Called
  from an ``admin_session`` (no tenant GUC: the request carries no
  Flow auth context). Looks up codes / chat ids via the SECURITY
  DEFINER helpers from migration 0053 so RLS does not block the
  cross-tenant operations the webhook needs (the webhook only ever
  acts on rows on behalf of the user the link/chat belongs to).

Incoming messages are turned into Notes (default) or Tasks (when the
message starts with ``/task``). They land in the user's "default
workspace" (their earliest membership: the personal workspace from
signup or the first they joined). RLS isolation is preserved because
the service opens a ``tenant_session`` keyed on that workspace before
calling the note/task service.
"""

from __future__ import annotations

import datetime
import logging
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.config import get_settings
from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode, render
from flow_core.models.note import NoteKind
from flow_core.models.telegram import TelegramLink, TelegramLinkCode, TelegramUpdate
from flow_core.services import attachments as att_svc
from flow_core.services import notes as notes_svc
from flow_core.services import tasks as tasks_svc
from flow_core.telegram_client import get_telegram_api

logger = logging.getLogger("flow.telegram")

# Single-use deep-link code lifetime. Short on purpose: the code is
# the only authentication factor in the linking handshake -- whoever
# types ``/start <code>`` to the bot becomes the link target. 15 min
# is plenty for "user clicks button -> opens Telegram"; longer would
# widen the window an intercepted code is useful for.
_CODE_TTL = datetime.timedelta(minutes=15)
_CODE_BYTES = 4  # 8 hex chars: 32 bits of entropy is enough for a 15-min single-use token


def _mint_code() -> str:
    """8-char hex token. ``secrets.token_hex`` is CSPRNG-backed; the
    1-in-4-billion collision odds are caught by the UNIQUE constraint
    (the mint loop in ``create_link_code`` retries on collision)."""
    return secrets.token_hex(_CODE_BYTES)


# ---------------------------------------------------------------------------
# Authenticated link flow (called from the API router with tenant ctx)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssuedLinkCode:
    code: str
    expires_at: datetime.datetime
    deep_link: str
    bot_username: str


async def create_link_code(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    bot_username: str,
) -> IssuedLinkCode:
    """Mint a fresh deep-link code for ``user_id`` in workspace
    ``org_id``. Soft-invalidates older unconsumed codes for the same
    user so only one live code is ever in flight. The bot username is
    passed in (not read from Settings here) so the service stays
    composable and tests do not need the env var; the router resolves
    it from ``get_settings``.

    Note: the deep link is built here for convenience; ``telegram_deep_link``
    in ``telegram_client`` is the same builder, used by callers that
    do not have a DB session in hand (e.g. the bootstrap CLI)."""
    from flow_core.telegram_client import telegram_deep_link

    if not bot_username:
        raise DomainError(MessageCode.TELEGRAM_NOT_CONFIGURED)
    now = datetime.datetime.now(tz=datetime.UTC)
    # Soft-invalidate any older unconsumed code for the same user.
    await session.execute(
        update(TelegramLinkCode)
        .where(
            TelegramLinkCode.user_id == user_id,
            TelegramLinkCode.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    # Mint; loop on the (vanishingly unlikely) UNIQUE collision.
    code = ""
    for _ in range(5):
        candidate = _mint_code()
        clash = (
            await session.execute(
                select(TelegramLinkCode.id).where(TelegramLinkCode.code == candidate)
            )
        ).scalar_one_or_none()
        if clash is None:
            code = candidate
            break
    else:
        raise RuntimeError("could not mint a unique telegram link code after 5 tries")
    expires_at = now + _CODE_TTL
    session.add(
        TelegramLinkCode(
            org_id=org_id,
            user_id=user_id,
            code=code,
            expires_at=expires_at,
        )
    )
    await session.flush()
    return IssuedLinkCode(
        code=code,
        expires_at=expires_at,
        deep_link=telegram_deep_link(bot_username=bot_username, code=code),
        bot_username=bot_username,
    )


@dataclass(frozen=True, slots=True)
class LinkStatus:
    linked: bool
    chat_username: str | None
    linked_at: datetime.datetime | None


async def get_link_status(session: AsyncSession, *, user_id: uuid.UUID) -> LinkStatus:
    """Read the user's Telegram link. RLS keys on
    ``app.current_user`` (set by ``tenant_session``) and the self
    policy from migration 0053 lets the user see their own row."""
    row = (
        await session.execute(select(TelegramLink).where(TelegramLink.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return LinkStatus(linked=False, chat_username=None, linked_at=None)
    return LinkStatus(
        linked=True,
        chat_username=row.chat_username,
        linked_at=row.linked_at,
    )


async def unlink(session: AsyncSession, *, user_id: uuid.UUID) -> bool:
    """Remove the user's Telegram link. Returns True iff a row was
    deleted (a no-op on a non-linked user is not an error)."""
    row = (
        await session.execute(select(TelegramLink).where(TelegramLink.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


# ---------------------------------------------------------------------------
# Webhook ingestion (called from the API router with admin_session)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpdateOutcome:
    """What the webhook should reply to the user in chat."""

    reply_text: str | None
    # The Note / Task created as a side effect (when applicable),
    # mostly for tests; the webhook itself ignores the value.
    note_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None


async def _is_duplicate_update(session: AsyncSession, *, update_id: int) -> bool:
    """Exactly-once delivery: persist Telegram's monotonically
    increasing update_id; a repeat is a no-op. Telegram retries
    failed deliveries aggressively, so this is load-bearing."""
    existing = (
        await session.execute(
            select(TelegramUpdate.update_id).where(TelegramUpdate.update_id == update_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return True
    session.add(TelegramUpdate(update_id=update_id))
    await session.flush()
    return False


_TASK_PREFIX = "/task"

# Bot replies live in the i18n catalog (ADR-0017). Rendered once at import
# (only the "en" locale exists). The assistant's own generated answer is
# passthrough, not catalogable.
_HELP_TEXT = render(MessageCode.TELEGRAM_HELP)
_FREETEXT_HINT = render(MessageCode.TELEGRAM_FREETEXT_HINT)


async def handle_webhook_update(payload: dict[str, object]) -> UpdateOutcome:
    """Top-level webhook entry. Idempotent by ``update_id``: a
    re-delivery of the same Telegram update is a no-op. Three cases:

    - ``/start <code>``: redeem the code via the SECURITY DEFINER
      function; on success, the link row is upserted atomically and
      we sync the user's telegram notification pref to target the
      new chat.
    - Any other message from a linked chat: route into the user's
      default workspace as a Note (or a Task when prefixed with
      ``/task``).
    - Unknown chat (no link): instruction reply so the user knows
      how to bind their account."""
    from flow_core.services.notifications_telegram import sync_pref_from_link

    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        # Malformed payload; cannot dedupe, so refuse. Telegram does
        # not retry malformed input.
        return UpdateOutcome(reply_text=None)

    # Dedupe FIRST in its own short admin_session so a re-delivery
    # is rejected even when the payload is otherwise non-message.
    async with admin_session() as s:
        if await _is_duplicate_update(s, update_id=update_id):
            return UpdateOutcome(reply_text=None)

    message = payload.get("message") or payload.get("edited_message")
    if not isinstance(message, dict):
        # Non-message updates (callback queries, channel posts, ...)
        # are recorded in the seen-set but otherwise ignored.
        return UpdateOutcome(reply_text=None)

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return UpdateOutcome(reply_text=None)
    chat_id_raw = chat.get("id")
    if not isinstance(chat_id_raw, int):
        return UpdateOutcome(reply_text=None)
    chat_id = chat_id_raw
    chat_username_raw = chat.get("username")
    chat_username = chat_username_raw if isinstance(chat_username_raw, str) else None

    raw_text = message.get("text")
    body = raw_text if isinstance(raw_text, str) else ""
    body_stripped = body.strip()

    # --- /start <code> ---------------------------------------------------
    if body_stripped.startswith("/start"):
        parts = body_stripped.split(maxsplit=1)
        code = parts[1].strip() if len(parts) == 2 else ""
        if not code:
            return UpdateOutcome(reply_text=render(MessageCode.TELEGRAM_START_WELCOME))
        async with admin_session() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT out_user_id, out_org_id FROM"
                        " consume_telegram_link_code(:c, :chat, :uname)"
                    ),
                    {"c": code, "chat": chat_id, "uname": chat_username},
                )
            ).first()
        if row is None or row.out_user_id is None:
            return UpdateOutcome(reply_text=render(MessageCode.TELEGRAM_CODE_INVALID))
        linked_user_id = row.out_user_id
        linked_org_id = row.out_org_id
        async with tenant_session(str(linked_org_id), str(linked_user_id)) as ts:
            await sync_pref_from_link(
                ts, org_id=linked_org_id, user_id=linked_user_id, chat_id=chat_id
            )
        return UpdateOutcome(
            reply_text=render(MessageCode.TELEGRAM_LINKED),
            user_id=linked_user_id,
        )

    # --- Regular message: route to the linked user's workspace ----------
    async with admin_session() as s:
        chat_lookup = (
            await s.execute(
                text("SELECT out_user_id, out_default_org_id FROM resolve_telegram_chat(:chat)"),
                {"chat": chat_id},
            )
        ).first()
    if chat_lookup is None or chat_lookup.out_user_id is None:
        return UpdateOutcome(reply_text=render(MessageCode.TELEGRAM_NOT_LINKED))
    user_id = chat_lookup.out_user_id
    org_id = chat_lookup.out_default_org_id
    if org_id is None:
        # Linked user with no workspace (a partial signup that the
        # bootstrap-admin self-heal would normally repair). Refuse
        # politely rather than 500.
        return UpdateOutcome(reply_text=render(MessageCode.TELEGRAM_NO_WORKSPACE))

    # Refresh the cached username if it changed (Telegram lets users
    # rename their @handle). Self policy on telegram_links allows the
    # owner to update their row, so we run it inside a per-user
    # tenant session.
    if chat_username is not None:
        async with tenant_session(str(org_id), str(user_id)) as ts:
            await ts.execute(
                update(TelegramLink)
                .where(
                    TelegramLink.user_id == user_id,
                    TelegramLink.chat_username != chat_username,
                )
                .values(chat_username=chat_username)
            )

    # --- Voice message ---------------------------------------------------
    # Telegram delivers voice messages as ``message.voice`` with file_id,
    # duration_seconds, mime_type ("audio/ogg" by convention). We pull
    # the bytes via the bot API (getFile -> /file/bot...), attach to a
    # new voice note, set audio_ref = ``attachment:<id>``, and trigger
    # transcription best-effort. If no STT provider is configured, the
    # transcribe call surfaces an error in the note's status; the audio
    # itself is still playable from /notes.
    voice = message.get("voice")
    if isinstance(voice, dict) and isinstance(voice.get("file_id"), str):
        file_id = voice["file_id"]
        duration = voice.get("duration")
        audio_seconds = int(duration) if isinstance(duration, int) else 0
        mime = voice.get("mime_type") or "audio/ogg"
        try:
            api_client = get_telegram_api()
            tg_path = await api_client.get_file_path(file_id=file_id)
            data = await api_client.download_file(file_path=tg_path)
        except Exception:
            logger.exception("telegram voice download failed for chat_id=%s", chat_id)
            return UpdateOutcome(
                reply_text=render(MessageCode.TELEGRAM_VOICE_FAILED),
                user_id=user_id,
            )
        async with tenant_session(str(org_id), str(user_id)) as ts:
            note = await notes_svc.create_note(
                ts,
                org_id=org_id,
                actor_id=user_id,
                kind=NoteKind.voice,
                title=None,
                text=None,
                audio_seconds=audio_seconds,
            )
            att = await att_svc.add_attachment(
                ts,
                org_id=org_id,
                actor_id=user_id,
                note_id=note.id,
                filename=f"telegram-{file_id[:16]}.ogg",
                mime_type=mime,
                data=data,
            )
            await notes_svc.update_note(
                ts,
                org_id=org_id,
                actor_id=user_id,
                note_id=note.id,
                expected_version=note.version,
                audio_ref=f"attachment:{att.id}",
            )
            try:
                await notes_svc.transcribe(
                    ts,
                    org_id=org_id,
                    actor_id=user_id,
                    note_id=note.id,
                    operation_id=f"telegram-{file_id}",
                    embed=True,
                )
            except Exception:
                # Best-effort: an unconfigured STT raises here; the
                # audio is still saved on the note for later replay.
                logger.exception("telegram voice transcribe failed for note=%s", note.id)
        return UpdateOutcome(
            reply_text=render(MessageCode.TELEGRAM_VOICE_SAVED),
            note_id=note.id,
            user_id=user_id,
        )

    if not body_stripped:
        return UpdateOutcome(
            reply_text=render(MessageCode.TELEGRAM_EMPTY_IGNORED),
            user_id=user_id,
        )

    # Slash-commands are handled explicitly. Telegram renders ``/foo`` as
    # a tappable command, so a user who sends one means a command, not
    # note text -- never silently store it as a note.
    if body_stripped.startswith("/"):
        # First token, lowercased, with the ``@botname`` suffix Telegram
        # appends in group chats stripped off.
        command = body_stripped.split(maxsplit=1)[0].lower().split("@", 1)[0]
        if command == _TASK_PREFIX:
            parts = body_stripped.split(maxsplit=1)
            title = (parts[1].strip() if len(parts) == 2 else "") or "Task from Telegram"
            async with tenant_session(str(org_id), str(user_id)) as ts:
                task = await tasks_svc.create_task(
                    ts,
                    org_id=org_id,
                    actor_id=user_id,
                    title=title[:300],
                    description=body_stripped if len(body_stripped) > 300 else None,
                )
            return UpdateOutcome(
                reply_text=render(MessageCode.TELEGRAM_TASK_CREATED, title=title[:80]),
                task_id=task.id,
                user_id=user_id,
            )
        if command == "/note":
            note_parts = body_stripped.split(maxsplit=1)
            text_body = note_parts[1].strip() if len(note_parts) == 2 else ""
            if not text_body:
                return UpdateOutcome(
                    reply_text=render(MessageCode.TELEGRAM_NOTE_USAGE), user_id=user_id
                )
            async with tenant_session(str(org_id), str(user_id)) as ts:
                note = await notes_svc.create_note(
                    ts,
                    org_id=org_id,
                    actor_id=user_id,
                    kind=NoteKind.text,
                    title=None,
                    text=text_body,
                )
            return UpdateOutcome(
                reply_text=render(MessageCode.TELEGRAM_NOTE_SAVED),
                note_id=note.id,
                user_id=user_id,
            )
        if command == "/help":
            return UpdateOutcome(reply_text=_HELP_TEXT, user_id=user_id)
        return UpdateOutcome(
            reply_text=render(
                MessageCode.TELEGRAM_UNKNOWN_COMMAND, command=command, help=_HELP_TEXT
            ),
            user_id=user_id,
        )

    # Free-text (no command). When the conversational assistant is enabled
    # (ADR-0026) the message is handled by the in-process LLM agent scoped
    # to this user's workspace; otherwise free-text is reserved for the
    # future assistant and we reply with guidance toward the commands.
    if get_settings().assistant_enabled:
        from flow_core.services import assistant as assistant_svc

        # Enqueue for the worker (ADR-0026 P3): a slow LLM turn must not
        # block the webhook reply (Telegram retries on timeout). The
        # worker runs the turn and sends the answer; we ack with no reply.
        async with admin_session() as s:
            await assistant_svc.enqueue_turn(
                s,
                org_id=org_id,
                user_id=user_id,
                chat_id=chat_id,
                update_id=update_id,
                text=body_stripped,
            )
        return UpdateOutcome(reply_text=None, user_id=user_id)
    return UpdateOutcome(reply_text=_FREETEXT_HINT, user_id=user_id)


async def get_link_for_user(user_id: uuid.UUID) -> TelegramLink | None:
    """Fetch a user's Telegram link from outside a tenant session
    (e.g. the notifications dispatcher, which iterates all users in
    a batch). Uses ``admin_session`` because the link is not
    org-scoped and the caller already authorised the read by being
    in the dispatcher (the dispatcher itself enforces tenant scope on
    the originating notification, not on the side-channel target)."""
    # NOTE: admin_session has no app.current_user GUC; the self-policy
    # on telegram_links would filter the SELECT out. We use the same
    # SECURITY DEFINER ``resolve_telegram_chat`` family of helpers --
    # but for the by-user direction the simplest path is the SQL
    # function inverse, which we add inline here via a small helper.
    async with admin_session() as s:
        # The owner-role function family already exposes the inverse
        # we need (chat_id by user) via the standing self policy
        # ``p_telegram_links_self`` once we set the user GUC. Set it
        # transaction-local (the same shape as tenant_session, just
        # only the user dimension; no tenant context is needed to
        # see one's own link row).
        await s.execute(
            text("SELECT set_config('app.current_user', :u, true)"),
            {"u": str(user_id)},
        )
        row = (
            await s.execute(select(TelegramLink).where(TelegramLink.user_id == user_id))
        ).scalar_one_or_none()
    return row


__all__ = [
    "IssuedLinkCode",
    "LinkStatus",
    "UpdateOutcome",
    "create_link_code",
    "get_link_for_user",
    "get_link_status",
    "handle_webhook_update",
    "unlink",
]
