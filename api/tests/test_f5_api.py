"""F5 API end-to-end (DB-backed): account CRUD (secret never echoed),
idempotent sync, email-to-task, send, cross-org isolation. The
connector factory is overridden with an in-memory one (ADR-0023 test
seam)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.email_connector import (
    FetchedMessage,
    OutgoingMessage,
    set_connector_override,
)


class FakeConnector:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def fetch(self, *, limit: int = 50) -> list[FetchedMessage]:
        return [
            FetchedMessage(
                provider_message_id="1",
                from_addr="alice@example.test",
                to_addrs=["me@example.test"],
                received_at=dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC),
                message_id="<1@example.test>",
                subject="Please handle",
                body_text="body text",
            )
        ]

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return "<sent-api@mycelium>"


@pytest.fixture
def _fake_connector() -> Iterator[FakeConnector]:
    fake = FakeConnector()
    set_connector_override(lambda _a, _s: fake)
    try:
        yield fake
    finally:
        set_connector_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f5_api_flow(_fake_connector: FakeConnector) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
            )
        ).json()
        b = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "B"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}

        created = await c.post(
            "/email/accounts",
            headers=h,
            json={
                "provider": "imap_generic",
                "email_address": "me@example.test",
                "secret": "super-secret-pw",
                "imap_host": "imap.example.test",
                "smtp_host": "smtp.example.test",
            },
        )
        assert created.status_code == 200
        acc = created.json()
        # The secret must never be echoed.
        assert "secret" not in acc and "super-secret-pw" not in created.text

        got = (await c.get(f"/email/accounts/{acc['id']}", headers=h)).json()
        assert "secret" not in got

        synced = await c.post(f"/email/accounts/{acc['id']}/sync", headers=h)
        assert synced.status_code == 200 and synced.json()["created"] == 1
        # Idempotent: a second sync creates nothing.
        again = await c.post(f"/email/accounts/{acc['id']}/sync", headers=h)
        assert again.json()["created"] == 0

        msgs = (await c.get("/email/messages", headers=h)).json()
        assert len(msgs) == 1
        mid = msgs[0]["id"]

        tt = await c.post(f"/email/messages/{mid}/to-task", headers=h, json={})
        assert tt.status_code == 200
        task_id = tt.json()["task_id"]
        relinked = (await c.get(f"/email/messages/{mid}", headers=h)).json()
        assert relinked["linked_task_id"] == task_id

        reply = await c.post(
            f"/email/messages/{mid}/reply",
            headers=h,
            json={"body_text": "thanks"},
        )
        assert reply.status_code == 200 and reply.json()["sent_id"]
        assert _fake_connector.sent[-1].subject == "Re: Please handle"

        cross = await c.get(
            "/email/accounts",
            headers={"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": b["workspace_id"]},
        )
        assert cross.status_code == 403
