"""A message to oneself (``POST /notifications/self``, MCP ``notify_self``).

What it must hold, end to end against Postgres:

- it reaches the caller on the caller's own reachable channels, and the
  Telegram copy goes to the chat the caller linked, nothing else;
- nobody can reach another person through it, whatever workspace they share;
- an agent token can use it (that is the point: a laptop or an agent tells
  its person something), and the MCP tool is the same operation;
- a key makes a repeat a no-op, nothing reachable is refused, and a loop is
  stopped by the hourly bound.

Observed failing before its guard existed (2026-11-27): with the parameter
model accepting extra keys, ``test_a_member_cannot_reach_another_member``
got 200 for a body carrying ``user_id``, which was dropped in silence.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient

os.environ["MYCELIUM_TELEGRAM_BOT_TOKEN"] = "test:token"
os.environ["MYCELIUM_TELEGRAM_BOT_USERNAME"] = "mycelium_test_bot"
os.environ["MYCELIUM_TELEGRAM_WEBHOOK_SECRET"] = "wh-secret-1234"

from mycelium_api.main import app
from mycelium_core.config import get_settings
from mycelium_core.db import tenant_session
from mycelium_core.models.notification import NotificationChannelKind
from mycelium_core.services import memberships as mem_svc
from mycelium_core.services import notifications as nf
from mycelium_core.telegram_client import (
    TelegramSendResult,
    TelegramSetWebhookResult,
    set_telegram_api_override,
)
from mycelium_mcp.tools_notifications import TOOLS


class FakeTelegramApi:
    """Recording fake of the whole ``TelegramApi`` protocol."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str) -> TelegramSendResult:
        self.sent.append((chat_id, text))
        return TelegramSendResult(message_id=len(self.sent))

    async def set_webhook(self, *, url: str, secret_token: str) -> TelegramSetWebhookResult:
        return TelegramSetWebhookResult(ok=True, description="ok")

    async def get_file_path(self, *, file_id: str) -> str:
        return f"voice/{file_id}.oga"

    async def download_file(self, *, file_path: str) -> bytes:
        return b""


class RecordingSender:
    """The notification sender seam, recording what dispatch delivers. It is
    the network edge of this test, where recording the call is the contract."""

    def __init__(self) -> None:
        self.sent: list[tuple[NotificationChannelKind, str, str]] = []

    async def send(
        self, *, channel: NotificationChannelKind, target: str, title: str, body: str
    ) -> None:
        self.sent.append((channel, target, title))


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_tg() -> Iterator[FakeTelegramApi]:
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
        json={"email": email, "password": "pw-strong-123", "workspace_name": "S"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _h(token: str, workspace_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Workspace-Id": workspace_id}


async def _dispatch(org: str, user: str) -> RecordingSender:
    snd = RecordingSender()
    async with tenant_session(org, user) as s:
        await nf.dispatch_pending(s, org_id=uuid.UUID(org), actor_id=uuid.UUID(user), sender=snd)
    return snd


async def _link_telegram(c: AsyncClient, h: dict[str, str], chat_id: int) -> None:
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


async def test_it_reaches_the_caller_on_their_own_channels(fake_tg: FakeTelegramApi) -> None:
    email = _email()
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, email)
        h = _h(a["token"], a["workspace_id"])
        await _link_telegram(c, h, chat_id)
        on = await c.put(
            "/notifications/prefs", headers=h, json={"channel": "telegram", "enabled": True}
        )
        assert on.status_code == 200, on.text
        r = await c.post(
            "/notifications/self", headers=h, json={"title": "AC unplugged", "body": "40%"}
        )
        assert r.status_code == 200, r.text
        assert sorted(r.json()["queued"]) == ["email", "telegram"]
    snd = await _dispatch(a["workspace_id"], a["user_id"])
    assert sorted(snd.sent) == sorted(
        [
            (NotificationChannelKind.email, email, "AC unplugged"),
            (NotificationChannelKind.telegram, str(chat_id), "AC unplugged"),
        ]
    )


async def test_a_member_cannot_reach_another_member() -> None:
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
        # B enables its own e-mail in A's workspace, then tries every way the
        # body could name someone else: none is accepted.
        hb = _h(b["token"], org)
        await c.put("/notifications/prefs", headers=hb, json={"channel": "email", "enabled": True})
        for extra in ({"user_id": a["user_id"]}, {"target": a_email}, {"to": a_email}):
            r = await c.post("/notifications/self", headers=hb, json={"title": "x", **extra})
            assert r.status_code == 422, r.text
        ok = await c.post("/notifications/self", headers=hb, json={"title": "mine"})
        assert ok.status_code == 200, ok.text
    snd = await _dispatch(org, a["user_id"])
    assert snd.sent == [(NotificationChannelKind.email, b_email, "mine")]


async def test_an_agent_token_can_message_its_person() -> None:
    email = _email()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, email)
        # Minting is owner-gated, and a fresh workspace's owner acts as member
        # unless it asks for the role (the sudo header model).
        minted = await c.post(
            "/agent-tokens",
            headers={**_h(a["token"], a["workspace_id"]), "X-Workspace-Role": "owner"},
            json={"name": "laptop"},
        )
        assert minted.status_code == 200, minted.text
        r = await c.post(
            "/notifications/self",
            headers=_h(minted.json()["raw"], a["workspace_id"]),
            json={"title": "battery 6%"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["queued"] == ["email"]


async def test_the_mcp_tool_is_the_same_operation() -> None:
    email = _email()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, email)
    out = await TOOLS["notify_self"](
        token=a["token"], org_id=a["workspace_id"], title="blocked on a permission"
    )
    assert out == {"queued": ["email"], "already_queued": []}
    snd = await _dispatch(a["workspace_id"], a["user_id"])
    assert snd.sent == [(NotificationChannelKind.email, email, "blocked on a permission")]


async def test_a_key_makes_a_repeat_a_no_op() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, _email())
        h = _h(a["token"], a["workspace_id"])
        body = {"title": "AC unplugged", "key": "ac-off-1"}
        first = (await c.post("/notifications/self", headers=h, json=body)).json()
        again = (await c.post("/notifications/self", headers=h, json=body)).json()
    assert first == {"queued": ["email"], "already_queued": []}
    assert again == {"queued": [], "already_queued": ["email"]}
    snd = await _dispatch(a["workspace_id"], a["user_id"])
    assert len(snd.sent) == 1


async def test_nothing_reachable_is_refused() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, _email())
        h = _h(a["token"], a["workspace_id"])
        await c.put("/notifications/prefs", headers=h, json={"channel": "email", "enabled": False})
        r = await c.post("/notifications/self", headers=h, json={"title": "anyone?"})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "notification.no_reachable_channel"


async def test_a_loop_is_stopped_by_the_hourly_bound() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _signup(c, _email())
        h = _h(a["token"], a["workspace_id"])
        for i in range(nf.SELF_NOTIFY_PER_HOUR):
            r = await c.post("/notifications/self", headers=h, json={"title": f"n{i}"})
            assert r.status_code == 200, r.text
        over = await c.post("/notifications/self", headers=h, json={"title": "one more"})
    assert over.status_code == 429, over.text
    assert over.json()["code"] == "notification.self_quota"
