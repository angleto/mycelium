"""F5 MCP co-equality (DB-backed): email tools reuse the same service
layer as REST (docs/adr/0001), with the connector factory overridden
by an in-memory one (ADR-0023 test seam)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest

from flow_core.db import admin_session
from flow_core.email_connector import (
    FetchedMessage,
    OutgoingMessage,
    set_connector_override,
)
from flow_core.services.auth import signup
from flow_mcp.server import (
    create_email_account,
    email_to_task,
    list_email_messages,
    send_email,
    sync_email_account,
)


class FakeConnector:
    async def fetch(self, *, limit: int = 50) -> list[FetchedMessage]:
        return [
            FetchedMessage(
                provider_message_id="1",
                from_addr="alice@example.test",
                to_addrs=["me@example.test"],
                received_at=dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC),
                message_id="<1@example.test>",
                subject="MCP mail",
                body_text="hello",
            )
        ]

    async def send(self, message: OutgoingMessage) -> str:
        return "<sent-mcp@flow>"


@pytest.fixture
def _override() -> Iterator[None]:
    set_connector_override(lambda _a, _s: FakeConnector())
    try:
        yield
    finally:
        set_connector_override(None)


async def test_mcp_email(_override: None) -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP5",
        )
    token, org = r.token, str(r.org_id)

    acc = await create_email_account(
        token=token,
        org_id=org,
        provider="imap_generic",
        email_address="me@example.test",
        secret="super-secret-pw",
        imap_host="imap.example.test",
        smtp_host="smtp.example.test",
    )
    assert "secret" not in acc

    synced = await sync_email_account(token=token, org_id=org, account_id=acc["id"])
    assert synced["created"] == 1
    msgs = await list_email_messages(token=token, org_id=org)
    assert len(msgs) == 1

    tt = await email_to_task(token=token, org_id=org, message_id=msgs[0]["id"])
    assert tt["task_id"]

    sent = await send_email(
        token=token,
        org_id=org,
        account_id=acc["id"],
        to_addrs=["bob@example.test"],
        subject="hi",
        body_text="body",
    )
    assert sent["sent_id"] == "<sent-mcp@flow>"
