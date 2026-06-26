"""F8 MCP co-equality (DB-backed): notification/recurrence tools reuse
the same service layer as REST (docs/adr/0001)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from mycelium_core.db import admin_session
from mycelium_core.models.notification import NotificationChannelKind
from mycelium_core.notification_channel import NotificationSender, set_sender_override
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    dispatch_notifications,
    set_notification_pref,
)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(
        self, *, channel: NotificationChannelKind, target: str, title: str, body: str
    ) -> None:
        self.sent.append(target)


@pytest.fixture
def _sender() -> Iterator[FakeSender]:
    snd = FakeSender()
    set_sender_override(lambda: snd)
    try:
        yield snd
    finally:
        set_sender_override(None)


def _assert_sender(_: NotificationSender) -> None: ...


async def test_mcp_notifications(_sender: FakeSender) -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP8",
        )
    token, org, me = r.token, str(r.org_id), str(r.user_id)

    await set_notification_pref(
        token=token,
        org_id=org,
        user_id=me,
        channel="email",
        target="me@example.test",
    )
    out = await dispatch_notifications(token=token, org_id=org)
    assert out == {"sent": 0, "failed": 0}  # nothing queued yet
