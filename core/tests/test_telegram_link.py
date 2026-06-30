"""Telegram bot integration (epic #125 P2) tests.

State-of-link transitions, idempotent webhook, ``/start <code>`` ->
TelegramLink created, ``/note ...`` -> Note created, ``/task ...`` ->
Task created, ``/help`` + unknown commands answered without storing,
free-text reserved for the future assistant (not stored), RLS isolation
(Telegram link belongs to user/org). Runs against the real DB + a fake
``TelegramApi``."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import Note
from mycelium_core.models.notification import NotificationChannelKind, NotificationPref
from mycelium_core.models.task import Task
from mycelium_core.models.telegram import TelegramLink, TelegramLinkCode, TelegramUpdate
from mycelium_core.services import telegram_link as svc
from mycelium_core.services.auth import signup
from mycelium_core.telegram_client import (
    TelegramApi,
    TelegramSendResult,
    TelegramSetWebhookResult,
    set_telegram_api_override,
    telegram_deep_link,
)


def _uid() -> int:
    """Random update_id big enough not to collide with the accreting
    seen-set (the table is global and survives across the suite)."""
    return uuid.uuid4().int & ((1 << 62) - 1)


class FakeTelegramApi:
    """Recording fake. The Protocol seam is the entire point: no
    monkey-patching, no httpx, no api.telegram.org."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.webhook: tuple[str, str] | None = None
        # Voice-note capture (file download seam): map file_id -> path
        # and path -> bytes so a test can stage a fake voice file.
        self.file_paths: dict[str, str] = {}
        self.files: dict[str, bytes] = {}

    async def send_message(self, *, chat_id: int, text: str) -> TelegramSendResult:
        self.sent.append((chat_id, text))
        return TelegramSendResult(message_id=len(self.sent))

    async def set_webhook(self, *, url: str, secret_token: str) -> TelegramSetWebhookResult:
        self.webhook = (url, secret_token)
        return TelegramSetWebhookResult(ok=True, description="ok")

    async def get_file_path(self, *, file_id: str) -> str:
        return self.file_paths.get(file_id, f"voice/{file_id}.oga")

    async def download_file(self, *, file_path: str) -> bytes:
        return self.files.get(file_path, b"")


def test_fake_satisfies_protocol() -> None:
    # Structural Protocol check. ``TelegramApi`` is @runtime_checkable,
    # so isinstance verifies every Protocol member is present: this fails
    # loudly if the fake drifts from the Protocol (e.g. a new method is
    # added to TelegramApi but not mirrored here). The bare hasattr that
    # used to stand here let exactly that drift through.
    api: TelegramApi = FakeTelegramApi()
    assert isinstance(api, TelegramApi)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="TG")
    return r.org_id, r.user_id


@pytest.fixture
def _fake_tg() -> Iterator[FakeTelegramApi]:
    api = FakeTelegramApi()
    set_telegram_api_override(lambda: api)
    try:
        yield api
    finally:
        set_telegram_api_override(None)


# ---------------------------------------------------------------------------
# Link minting + status + unlink (authenticated, tenant_session)
# ---------------------------------------------------------------------------


async def test_create_link_code_returns_deep_link_and_invalidates_previous() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        first = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
        assert first.deep_link == telegram_deep_link(
            bot_username="mycelium_test_bot", code=first.code
        )
        # Second mint invalidates the first (consumed_at set on the
        # previous unconsumed row).
        second = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
        assert second.code != first.code
        rows = (
            (await s.execute(select(TelegramLinkCode).where(TelegramLinkCode.user_id == user)))
            .scalars()
            .all()
        )
        consumed = [r for r in rows if r.consumed_at is not None]
        assert len(consumed) == 1 and consumed[0].code == first.code


async def test_link_status_starts_false_and_unlink_is_noop_without_link() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        status = await svc.get_link_status(s, user_id=user)
        assert status.linked is False
        assert await svc.unlink(s, user_id=user) is False


# ---------------------------------------------------------------------------
# Webhook redemption + ingestion
# ---------------------------------------------------------------------------


def _start_update(
    *, update_id: int, code: str, chat_id: int, username: str | None = "tg_user"
) -> dict[str, object]:
    chat: dict[str, object] = {"id": chat_id, "type": "private"}
    if username is not None:
        chat["username"] = username
    return {
        "update_id": update_id,
        "message": {"chat": chat, "text": f"/start {code}"},
    }


def _text_update(*, update_id: int, chat_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id, "type": "private"}, "text": text},
    }


def _voice_update(
    *, update_id: int, chat_id: int, file_id: str, caption: str | None = None
) -> dict[str, object]:
    message: dict[str, object] = {
        "chat": {"id": chat_id, "type": "private"},
        "voice": {"file_id": file_id, "duration": 3, "mime_type": "audio/ogg"},
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


async def test_start_with_valid_code_links_and_syncs_pref(_fake_tg: FakeTelegramApi) -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )

    chat_id = uuid.uuid4().int & 0xFFFFFFFF  # uniqueness across test runs
    outcome = await svc.handle_webhook_update(
        _start_update(update_id=_uid(), code=issued.code, chat_id=chat_id)
    )
    assert outcome.user_id == user
    assert outcome.reply_text is not None and "linked" in outcome.reply_text

    async with tenant_session(str(org), str(user)) as s:
        link = (
            await s.execute(select(TelegramLink).where(TelegramLink.user_id == user))
        ).scalar_one()
        assert link.chat_id == chat_id and link.chat_username == "tg_user"

        pref = (
            await s.execute(
                select(NotificationPref).where(
                    NotificationPref.user_id == user,
                    NotificationPref.channel == NotificationChannelKind.telegram,
                )
            )
        ).scalar_one()
        assert pref.enabled is True and pref.target == str(chat_id)


async def test_webhook_is_idempotent_by_update_id() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
    update_id = _uid()
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    payload = _start_update(update_id=update_id, code=issued.code, chat_id=chat_id)
    first = await svc.handle_webhook_update(payload)
    second = await svc.handle_webhook_update(payload)
    assert first.user_id == user
    assert second.reply_text is None  # replay no-ops
    async with admin_session() as s:
        seen = (
            await s.execute(select(TelegramUpdate).where(TelegramUpdate.update_id == update_id))
        ).scalar_one()
        assert seen.update_id == update_id


async def test_start_with_invalid_code_does_not_link() -> None:
    outcome = await svc.handle_webhook_update(
        _start_update(update_id=_uid(), code="deadbeef", chat_id=10)
    )
    assert outcome.user_id is None
    assert outcome.reply_text is not None and "invalid" in outcome.reply_text.lower()


async def test_note_command_creates_note() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    await svc.handle_webhook_update(
        _start_update(update_id=_uid(), code=issued.code, chat_id=chat_id)
    )
    outcome = await svc.handle_webhook_update(
        _text_update(update_id=_uid(), chat_id=chat_id, text="/note Pay invoice tomorrow")
    )
    assert outcome.note_id is not None and outcome.task_id is None
    async with tenant_session(str(org), str(user)) as s:
        note = (await s.execute(select(Note).where(Note.id == outcome.note_id))).scalar_one()
        # Phase 6 final: body lives in note_part(ord=0).
        from mycelium_core.services.notes import get_body as _get_body

        assert (await _get_body(s, note_id=note.id)) == "Pay invoice tomorrow"


async def test_plain_text_is_not_stored_and_returns_hint() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    await svc.handle_webhook_update(
        _start_update(update_id=_uid(), code=issued.code, chat_id=chat_id)
    )
    outcome = await svc.handle_webhook_update(
        _text_update(update_id=_uid(), chat_id=chat_id, text="just chatting")
    )
    # Free-text is reserved for the future assistant: no note, just guidance.
    assert outcome.note_id is None and outcome.task_id is None
    assert outcome.reply_text is not None and "/note" in outcome.reply_text


async def test_task_prefix_creates_task_not_note() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    await svc.handle_webhook_update(
        _start_update(update_id=_uid(), code=issued.code, chat_id=chat_id)
    )
    outcome = await svc.handle_webhook_update(
        _text_update(update_id=_uid(), chat_id=chat_id, text="/task Call client")
    )
    assert outcome.task_id is not None and outcome.note_id is None
    async with tenant_session(str(org), str(user)) as s:
        task = (await s.execute(select(Task).where(Task.id == outcome.task_id))).scalar_one()
        assert task.title == "Call client"


async def test_help_command_replies_without_storing() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    await svc.handle_webhook_update(
        _start_update(update_id=_uid(), code=issued.code, chat_id=chat_id)
    )
    outcome = await svc.handle_webhook_update(
        _text_update(update_id=_uid(), chat_id=chat_id, text="/help")
    )
    # /help is answered, not swallowed as a note or task.
    assert outcome.note_id is None and outcome.task_id is None
    assert outcome.reply_text is not None and "/task" in outcome.reply_text


async def test_unknown_command_is_not_saved_as_note() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    await svc.handle_webhook_update(
        _start_update(update_id=_uid(), code=issued.code, chat_id=chat_id)
    )
    outcome = await svc.handle_webhook_update(
        _text_update(update_id=_uid(), chat_id=chat_id, text="/frobnicate now")
    )
    assert outcome.note_id is None and outcome.task_id is None
    assert outcome.reply_text is not None and "unknown command" in outcome.reply_text.lower()


async def test_unknown_chat_returns_instruction() -> None:
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    outcome = await svc.handle_webhook_update(
        _text_update(update_id=_uid(), chat_id=chat_id, text="hello")
    )
    assert outcome.user_id is None
    assert outcome.reply_text is not None and "not linked" in outcome.reply_text.lower()


# ---------------------------------------------------------------------------
# RLS isolation -- a user cannot see another user's link
# ---------------------------------------------------------------------------


async def test_link_is_user_scoped_other_user_cannot_see() -> None:
    org_a, user_a = await _signup()
    org_b, user_b = await _signup()
    async with tenant_session(str(org_a), str(user_a)) as s:
        issued = await svc.create_link_code(
            s, org_id=org_a, user_id=user_a, bot_username="mycelium_test_bot"
        )
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    await svc.handle_webhook_update(
        _start_update(update_id=_uid(), code=issued.code, chat_id=chat_id)
    )
    async with tenant_session(str(org_b), str(user_b)) as s:
        status = await svc.get_link_status(s, user_id=user_a)
        assert status.linked is False
        rows = (await s.execute(select(TelegramLink))).scalars().all()
        assert rows == []


async def test_link_codes_are_org_isolated() -> None:
    org_a, user_a = await _signup()
    org_b, user_b = await _signup()
    async with tenant_session(str(org_a), str(user_a)) as s:
        await svc.create_link_code(
            s, org_id=org_a, user_id=user_a, bot_username="mycelium_test_bot"
        )
    async with tenant_session(str(org_b), str(user_b)) as s:
        rows = (await s.execute(select(TelegramLinkCode))).scalars().all()
        assert rows == []


# ---------------------------------------------------------------------------
# Outgoing notification via the Telegram sender
# ---------------------------------------------------------------------------


async def test_voice_transcription_failure_tells_user(_fake_tg: FakeTelegramApi) -> None:
    """When STT is unavailable (e.g. the faster-whisper extra is missing
    from the image, task 44ba3f14), the voice note is still saved and the
    reply says transcription is unavailable instead of implying success.
    Any transcribe failure (STT or metering) takes this branch."""
    from mycelium_core.ai_providers import set_stt_override

    class _RaisingSTT:
        model_id = "raising-stt"

        async def transcribe(self, **_: object) -> object:
            raise RuntimeError("STT unavailable")

    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    await svc.handle_webhook_update(
        _start_update(update_id=_uid(), code=issued.code, chat_id=chat_id)
    )
    _fake_tg.files["voice/vf1.oga"] = b"fake-ogg-bytes"

    set_stt_override(lambda: _RaisingSTT())  # type: ignore[arg-type,return-value]
    try:
        outcome = await svc.handle_webhook_update(
            _voice_update(update_id=_uid(), chat_id=chat_id, file_id="vf1")
        )
    finally:
        set_stt_override(None)

    assert outcome.reply_text is not None
    assert "transcription is unavailable" in outcome.reply_text
    # The audio is still saved as a note for later replay.
    assert outcome.note_id is not None


# ---------------------------------------------------------------------------
# Voice happy path: caption-driven note/task routing (task 44ba3f14,
# tests task bf0cc9d1). Runs with the deterministic FakeSTT and seeded
# billing (transcription is metered via the strict ``meter``).
# ---------------------------------------------------------------------------


async def _linked_user(_fake_tg: FakeTelegramApi) -> tuple[uuid.UUID, uuid.UUID, int]:
    """Signup + mint + redeem: a user with a linked Telegram chat and a
    staged fake voice file (file_id ``vf1``)."""
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="mycelium_test_bot"
        )
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    await svc.handle_webhook_update(
        _start_update(update_id=_uid(), code=issued.code, chat_id=chat_id)
    )
    _fake_tg.files["voice/vf1.oga"] = b"fake-ogg-bytes"
    return org, user, chat_id


async def _seed_stt_billing(org: uuid.UUID, user: uuid.UUID) -> None:
    """Credits + a fake-stt rate card: ``notes.transcribe`` meters via
    the strict ``billing.meter``, which errors on a missing card."""
    from decimal import Decimal

    from mycelium_core.services import billing

    async with tenant_session(str(org), str(user)) as s:
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(1000))
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id="fake-stt",
            provider="local",
            values={
                "credits_per_input": Decimal("0.001"),
                "credits_per_output": Decimal("0.001"),
            },
        )


@pytest.fixture
def _fake_stt() -> Iterator[None]:
    from _fake_ai import FakeSTT

    from mycelium_core.ai_providers import set_stt_override

    set_stt_override(FakeSTT)
    try:
        yield
    finally:
        set_stt_override(None)


async def test_voice_without_caption_creates_voice_note_with_transcript(
    _fake_tg: FakeTelegramApi, _fake_stt: None
) -> None:
    from mycelium_core.models.note import NoteKind, NoteStatus
    from mycelium_core.services.notes import get_body

    org, user, chat_id = await _linked_user(_fake_tg)
    await _seed_stt_billing(org, user)

    outcome = await svc.handle_webhook_update(
        _voice_update(update_id=_uid(), chat_id=chat_id, file_id="vf1")
    )
    assert outcome.note_id is not None and outcome.task_id is None
    assert outcome.reply_text is not None
    assert "transcription is unavailable" not in outcome.reply_text
    async with tenant_session(str(org), str(user)) as s:
        note = (await s.execute(select(Note).where(Note.id == outcome.note_id))).scalar_one()
        assert note.kind is NoteKind.voice
        assert note.status is NoteStatus.ready
        assert note.audio_ref is not None and note.audio_ref.startswith("attachment:")
        assert note.promoted_at is None
        # FakeSTT transcript lands in note_part(ord=0).
        assert (await get_body(s, note_id=note.id)) == f"transcript of {note.audio_ref}"


@pytest.mark.parametrize(
    "caption",
    ["task: Comprare il latte", "t: Comprare il latte", "/task Comprare il latte"],
)
async def test_voice_caption_task_prefix_promotes_to_task(
    _fake_tg: FakeTelegramApi, _fake_stt: None, caption: str
) -> None:
    org, user, chat_id = await _linked_user(_fake_tg)
    await _seed_stt_billing(org, user)

    outcome = await svc.handle_webhook_update(
        _voice_update(update_id=_uid(), chat_id=chat_id, file_id="vf1", caption=caption)
    )
    assert outcome.task_id is not None
    async with tenant_session(str(org), str(user)) as s:
        task = (await s.execute(select(Task).where(Task.id == outcome.task_id))).scalar_one()
        # The routing prefix is stripped; the rest is the title hint.
        assert task.title == "Comprare il latte"
        # The voice note survives as the promoted source of the task.
        notes = (await s.execute(select(Note))).scalars().all()
        promoted = [n for n in notes if n.promoted_at is not None]
        assert len(promoted) == 1


async def test_voice_caption_bare_task_titles_from_transcript_first_line(
    _fake_tg: FakeTelegramApi, _fake_stt: None
) -> None:
    org, user, chat_id = await _linked_user(_fake_tg)
    await _seed_stt_billing(org, user)

    outcome = await svc.handle_webhook_update(
        _voice_update(update_id=_uid(), chat_id=chat_id, file_id="vf1", caption="task")
    )
    assert outcome.task_id is not None
    async with tenant_session(str(org), str(user)) as s:
        task = (await s.execute(select(Task).where(Task.id == outcome.task_id))).scalar_one()
        # No title in the caption -> first transcript line wins.
        assert task.title.startswith("transcript of attachment:")


async def test_voice_caption_task_without_transcript_uses_placeholder(
    _fake_tg: FakeTelegramApi,
) -> None:
    """STT down + caption ``task`` with no title text: the promotion
    still happens (the task is the user's explicit intent) and the
    title falls back to the generic placeholder."""
    from mycelium_core.ai_providers import set_stt_override

    class _RaisingSTT:
        model_id = "raising-stt"

        async def transcribe(self, **_: object) -> object:
            raise RuntimeError("STT unavailable")

    org, user, chat_id = await _linked_user(_fake_tg)

    set_stt_override(lambda: _RaisingSTT())  # type: ignore[arg-type,return-value]
    try:
        outcome = await svc.handle_webhook_update(
            _voice_update(update_id=_uid(), chat_id=chat_id, file_id="vf1", caption="task")
        )
    finally:
        set_stt_override(None)

    assert outcome.task_id is not None
    async with tenant_session(str(org), str(user)) as s:
        task = (await s.execute(select(Task).where(Task.id == outcome.task_id))).scalar_one()
        assert task.title == "Voice task from Telegram"


async def test_voice_caption_without_prefix_stays_note(
    _fake_tg: FakeTelegramApi, _fake_stt: None
) -> None:
    org, user, chat_id = await _linked_user(_fake_tg)
    await _seed_stt_billing(org, user)

    outcome = await svc.handle_webhook_update(
        _voice_update(update_id=_uid(), chat_id=chat_id, file_id="vf1", caption="promemoria latte")
    )
    # A caption that does not start with a task prefix must never
    # promote: the clip stays a plain voice note.
    assert outcome.note_id is not None and outcome.task_id is None


async def test_telegram_notification_sender_calls_api(_fake_tg: FakeTelegramApi) -> None:
    from mycelium_core.services.notifications_telegram import TelegramNotificationSender

    class FallbackSender:
        async def send(
            self,
            *,
            channel: NotificationChannelKind,
            target: str,
            title: str,
            body: str,
        ) -> None:
            raise AssertionError("fallback should not be called for telegram")

    sender = TelegramNotificationSender(fallback=FallbackSender())
    await sender.send(
        channel=NotificationChannelKind.telegram,
        target="98765",
        title="Heads up",
        body="Pay invoice",
    )
    assert _fake_tg.sent == [(98765, "Heads up\n\nPay invoice")]


async def test_telegram_notification_sender_rejects_invalid_target(
    _fake_tg: FakeTelegramApi,
) -> None:
    from mycelium_core.services.notifications_telegram import TelegramNotificationSender

    class _Noop:
        async def send(self, **_: object) -> None:
            return None

    sender = TelegramNotificationSender(fallback=_Noop())
    with pytest.raises(RuntimeError, match="invalid telegram target"):
        await sender.send(
            channel=NotificationChannelKind.telegram,
            target="not-a-number",
            title="t",
            body="b",
        )
