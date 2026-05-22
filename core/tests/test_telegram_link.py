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

from flow_core.db import admin_session, tenant_session
from flow_core.models.note import Note
from flow_core.models.notification import NotificationChannelKind, NotificationPref
from flow_core.models.task import Task
from flow_core.models.telegram import TelegramLink, TelegramLinkCode, TelegramUpdate
from flow_core.services import telegram_link as svc
from flow_core.services.auth import signup
from flow_core.telegram_client import (
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
            s, org_id=org, user_id=user, bot_username="flow_test_bot"
        )
        assert first.deep_link == telegram_deep_link(bot_username="flow_test_bot", code=first.code)
        # Second mint invalidates the first (consumed_at set on the
        # previous unconsumed row).
        second = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="flow_test_bot"
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


async def test_start_with_valid_code_links_and_syncs_pref(_fake_tg: FakeTelegramApi) -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="flow_test_bot"
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
            s, org_id=org, user_id=user, bot_username="flow_test_bot"
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
            s, org_id=org, user_id=user, bot_username="flow_test_bot"
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
        assert note.transcript == "Pay invoice tomorrow"


async def test_plain_text_is_not_stored_and_returns_hint() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        issued = await svc.create_link_code(
            s, org_id=org, user_id=user, bot_username="flow_test_bot"
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
            s, org_id=org, user_id=user, bot_username="flow_test_bot"
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
            s, org_id=org, user_id=user, bot_username="flow_test_bot"
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
            s, org_id=org, user_id=user, bot_username="flow_test_bot"
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
            s, org_id=org_a, user_id=user_a, bot_username="flow_test_bot"
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
        await svc.create_link_code(s, org_id=org_a, user_id=user_a, bot_username="flow_test_bot")
    async with tenant_session(str(org_b), str(user_b)) as s:
        rows = (await s.execute(select(TelegramLinkCode))).scalars().all()
        assert rows == []


# ---------------------------------------------------------------------------
# Outgoing notification via the Telegram sender
# ---------------------------------------------------------------------------


async def test_telegram_notification_sender_calls_api(_fake_tg: FakeTelegramApi) -> None:
    from flow_core.services.notifications_telegram import TelegramNotificationSender

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
    from flow_core.services.notifications_telegram import TelegramNotificationSender

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
