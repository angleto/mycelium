"""Telegram bot router (epic #125 P2) HTTP end-to-end.

Covers link-mint / status / unlink on the authenticated surface and
the public webhook (path secret + optional header secret + 404 when
the bot is not configured). Real DB + fake ``TelegramApi`` injected
via the existing override seam (no httpx mocking)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient

# Required before importing the app: the webhook router consults
# Settings at request-time, so the env must be in place at module
# import time too (Settings is a cached singleton).
os.environ["MYCELIUM_TELEGRAM_BOT_TOKEN"] = "test:token"
os.environ["MYCELIUM_TELEGRAM_BOT_USERNAME"] = "mycelium_test_bot"
os.environ["MYCELIUM_TELEGRAM_WEBHOOK_SECRET"] = "wh-secret-1234"

from mycelium_api.main import app
from mycelium_core.config import get_settings
from mycelium_core.telegram_client import (
    TelegramSendResult,
    TelegramSetWebhookResult,
    set_telegram_api_override,
)


class FakeTelegramApi:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str) -> TelegramSendResult:
        self.sent.append((chat_id, text))
        return TelegramSendResult(message_id=len(self.sent))

    async def set_webhook(self, *, url: str, secret_token: str) -> TelegramSetWebhookResult:
        return TelegramSetWebhookResult(ok=True, description="ok")


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _fake_tg() -> Iterator[FakeTelegramApi]:
    api = FakeTelegramApi()
    set_telegram_api_override(lambda: api)
    try:
        yield api
    finally:
        set_telegram_api_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_link_request_returns_deep_link(_fake_tg: FakeTelegramApi) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        su = (
            await c.post(
                "/auth/signup",
                json={
                    "email": _email(),
                    "password": "pw-strong-123",
                    "workspace_name": "T",
                },
            )
        ).json()
        headers = {
            "Authorization": f"Bearer {su['token']}",
            "X-Workspace-Id": su["workspace_id"],
        }
        resp = await c.post("/telegram/link/request", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["bot_username"] == "mycelium_test_bot"
        assert body["deep_link"].startswith("https://t.me/mycelium_test_bot?start=")
        assert body["code"] in body["deep_link"]


async def test_link_status_reflects_redeemed_code(_fake_tg: FakeTelegramApi) -> None:
    transport = ASGITransport(app=app)
    chat_id = uuid.uuid4().int & 0xFFFFFFFF
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        su = (
            await c.post(
                "/auth/signup",
                json={
                    "email": _email(),
                    "password": "pw-strong-123",
                    "workspace_name": "T",
                },
            )
        ).json()
        headers = {
            "Authorization": f"Bearer {su['token']}",
            "X-Workspace-Id": su["workspace_id"],
        }
        issued = (await c.post("/telegram/link/request", headers=headers)).json()

        status = (await c.get("/telegram/link/status", headers=headers)).json()
        assert status["linked"] is False

        wh = await c.post(
            "/telegram/webhook/wh-secret-1234",
            json={
                "update_id": uuid.uuid4().int & ((1 << 62) - 1),
                "message": {
                    "chat": {"id": chat_id, "type": "private", "username": "test_user"},
                    "text": f"/start {issued['code']}",
                },
            },
        )
        assert wh.status_code == 200, wh.text
        assert wh.json() == {"ok": True}

        status = (await c.get("/telegram/link/status", headers=headers)).json()
        assert status["linked"] is True
        assert status["chat_username"] == "test_user"

        assert _fake_tg.sent and _fake_tg.sent[0][0] == chat_id

        d = await c.delete("/telegram/link", headers=headers)
        assert d.status_code == 204
        status = (await c.get("/telegram/link/status", headers=headers)).json()
        assert status["linked"] is False


async def test_webhook_rejects_bad_secret(_fake_tg: FakeTelegramApi) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        wh = await c.post(
            "/telegram/webhook/wrong-secret",
            json={
                "update_id": uuid.uuid4().int & ((1 << 62) - 1),
                "message": {"chat": {"id": 1}, "text": "/start x"},
            },
        )
        assert wh.status_code == 403


async def test_webhook_rejects_bad_header_secret(_fake_tg: FakeTelegramApi) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        wh = await c.post(
            "/telegram/webhook/wh-secret-1234",
            headers={"X-Telegram-Bot-Api-Secret-Token": "different"},
            json={
                "update_id": uuid.uuid4().int & ((1 << 62) - 1),
                "message": {"chat": {"id": 1}, "text": "/start x"},
            },
        )
        assert wh.status_code == 403


async def test_webhook_404s_when_telegram_not_configured() -> None:
    # Disable Telegram via env + cache reset for the body of this test.
    os.environ.pop("MYCELIUM_TELEGRAM_BOT_TOKEN", None)
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            wh = await c.post(
                "/telegram/webhook/wh-secret-1234",
                json={
                    "update_id": uuid.uuid4().int & ((1 << 62) - 1),
                    "message": {"chat": {"id": 1}, "text": "/start x"},
                },
            )
            assert wh.status_code == 404
    finally:
        os.environ["MYCELIUM_TELEGRAM_BOT_TOKEN"] = "test:token"
        get_settings.cache_clear()
