"""A notification preference belongs to the person it notifies, and its
destination is an identity that person proved.

Two defects, found 2026-11-27, which this file pins:

1. ``PUT /notifications/prefs`` (and the MCP ``set_notification_pref``)
   took the ``user_id`` from the request and checked only that the actor
   was a member. Any member could rewrite another member's preference,
   destination included, and from then on receive that person's
   reminders.
2. The destination (``target``) was free text, so even one's OWN
   preference could be pointed at an address or a Telegram chat nobody
   verified. Every notification then left the system towards a third
   party, and an agent token holding ``notifications:write`` could do it
   unattended. A notification delivered "to the caller's own channel" is
   only that while the channel is one the caller proved control of.

The fix makes the destination server-derived: the account e-mail for the
email channel, the linked chat for Telegram, nothing for web push. The
request carries only the channel and the on/off switch.

Observed failing before the fix (2.4 at d56b97d7, 2026-11-27, against
Postgres 16): the cross-member write stored ``attacker@example.test`` as
the other member's target; one's own target took the same free text; the
Telegram and MCP tests failed on the old contract (``user_id``/``target``
required). ``test_a_request_naming_a_destination_is_refused`` passed before
the fix, see its comment.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient

# The webhook router reads Settings at request time and Settings is a cached
# singleton, so the bot must be configured before the app is imported.
os.environ["MYCELIUM_TELEGRAM_BOT_TOKEN"] = "test:token"
os.environ["MYCELIUM_TELEGRAM_BOT_USERNAME"] = "mycelium_test_bot"
os.environ["MYCELIUM_TELEGRAM_WEBHOOK_SECRET"] = "wh-secret-1234"

from mycelium_api.main import app
from mycelium_core.config import get_settings
from mycelium_core.db import tenant_session
from mycelium_core.services import memberships as mem_svc
from mycelium_core.telegram_client import (
    TelegramSendResult,
    TelegramSetWebhookResult,
    set_telegram_api_override,
)
from mycelium_mcp.server import set_notification_pref

ATTACKER = "attacker@example.test"


class FakeTelegramApi:
    """Recording fake of the whole ``TelegramApi`` protocol. Linking a chat
    makes the bot send a welcome message, so the send seam is exercised."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.webhook: tuple[str, str] | None = None

    async def send_message(self, *, chat_id: int, text: str) -> TelegramSendResult:
        self.sent.append((chat_id, text))
        return TelegramSendResult(message_id=len(self.sent))

    async def set_webhook(self, *, url: str, secret_token: str) -> TelegramSetWebhookResult:
        self.webhook = (url, secret_token)
        return TelegramSetWebhookResult(ok=True, description="ok")

    async def get_file_path(self, *, file_id: str) -> str:
        return f"voice/{file_id}.oga"

    async def download_file(self, *, file_path: str) -> bytes:
        return b""


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fake_tg() -> Iterator[FakeTelegramApi]:
    api = FakeTelegramApi()
    set_telegram_api_override(lambda: api)
    try:
        yield api
    finally:
        set_telegram_api_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient, email: str) -> dict[str, str]:
    r = await c.post(
        "/auth/signup",
        json={"email": email, "password": "pw-strong-123", "workspace_name": "P"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _h(token: str, workspace_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Workspace-Id": workspace_id}


async def _prefs(c: AsyncClient, headers: dict[str, str]) -> dict[str, dict[str, object]]:
    r = await c.get("/notifications/prefs", headers=headers)
    assert r.status_code == 200, r.text
    return {p["channel"]: p for p in r.json()}


async def test_a_member_cannot_rewrite_another_members_pref() -> None:
    a_email, b_email = _email(), _email()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, a_email)
        b = await _signup(c, b_email)
        org = a["workspace_id"]
        async with tenant_session(org, a["user_id"]) as s:
            await mem_svc.add_member(
                s,
                org_id=uuid.UUID(org),
                actor_id=uuid.UUID(a["user_id"]),
                email=b_email,
                role="member",
            )
        await c.put(
            "/notifications/prefs",
            headers=_h(b["token"], org),
            json={"user_id": a["user_id"], "channel": "email", "enabled": True, "target": ATTACKER},
        )
        a_prefs = await _prefs(c, _h(a["token"], org))
    # Whatever the status code, A's destination must still be A's own
    # address: that is the property, the response is only its symptom.
    assert a_prefs["email"]["target"] == a_email


async def test_the_destination_is_not_free_text_even_for_oneself() -> None:
    email = _email()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, email)
        h = _h(a["token"], a["workspace_id"])
        await c.put(
            "/notifications/prefs",
            headers=h,
            json={"user_id": a["user_id"], "channel": "email", "enabled": True, "target": ATTACKER},
        )
        assert (await _prefs(c, h))["email"]["target"] == email
        r = await c.put(
            "/notifications/prefs", headers=h, json={"channel": "email", "enabled": True}
        )
        assert r.status_code == 200, r.text
        assert r.json()["target"] == email
        assert (await _prefs(c, h))["email"]["target"] == email


async def test_a_request_naming_a_destination_is_refused() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, _email())
        r = await c.put(
            "/notifications/prefs",
            headers=_h(a["token"], a["workspace_id"]),
            json={"channel": "email", "enabled": True, "target": ATTACKER},
        )
    # Refused rather than ignored: a client that still sends a target must
    # learn that it no longer chooses one, not believe it did. This one pins
    # the contract, not the defect: it passed before the fix too, because the
    # old schema refused the body for lacking a user_id.
    assert r.status_code == 422, r.text


async def test_telegram_cannot_be_enabled_without_a_link() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, _email())
        r = await c.put(
            "/notifications/prefs",
            headers=_h(a["token"], a["workspace_id"]),
            json={"channel": "telegram", "enabled": True},
        )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "notification.channel_not_linked"


async def test_telegram_destination_is_the_linked_chat(_fake_tg: FakeTelegramApi) -> None:
    # Requesting the fixture by name, although it is autouse, is what lets the
    # last assertion read what the bot sent.
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, _email())
        h = _h(a["token"], a["workspace_id"])
        issued = (await c.post("/telegram/link/request", headers=h)).json()
        wh = await c.post(
            "/telegram/webhook/wh-secret-1234",
            json={
                "update_id": uuid.uuid4().int & ((1 << 62) - 1),
                "message": {
                    "chat": {"id": chat_id, "type": "private", "username": "u"},
                    "text": f"/start {issued['code']}",
                },
            },
        )
        assert wh.status_code == 200, wh.text
        off = await c.put(
            "/notifications/prefs", headers=h, json={"channel": "telegram", "enabled": False}
        )
        assert off.status_code == 200, off.text
        on = await c.put(
            "/notifications/prefs", headers=h, json={"channel": "telegram", "enabled": True}
        )
        assert on.status_code == 200, on.text
        assert on.json()["target"] == str(chat_id)
    assert _fake_tg.sent and _fake_tg.sent[0][0] == chat_id


async def test_mcp_writes_only_the_callers_own_pref() -> None:
    a_email, b_email = _email(), _email()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, a_email)
        b = await _signup(c, b_email)
        org = a["workspace_id"]
        async with tenant_session(org, a["user_id"]) as s:
            await mem_svc.add_member(
                s,
                org_id=uuid.UUID(org),
                actor_id=uuid.UUID(a["user_id"]),
                email=b_email,
                role="member",
            )
        out = await set_notification_pref(
            token=b["token"], org_id=org, channel="email", enabled=False
        )
        assert out == {"channel": "email", "enabled": False, "target": b_email}
        a_prefs = await _prefs(c, _h(a["token"], org))
    assert a_prefs["email"]["target"] == a_email
    assert a_prefs["email"]["enabled"] is True
