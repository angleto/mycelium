"""Email-sync worker loop (docs/adr/0023, FR-7).

The service (``sync_all_accounts``) is covered by test_f5_email; this file
covers the *loop wiring*: ``run_once`` enumerates workspaces as a system
actor and runs the sweep inside an owner-authority ``tenant_session`` (so
``require_role(member)`` is satisfied and RLS shows the account). The
IMAP/SMTP boundary is the injected connector seam: the global
``set_connector_override`` feeds a fake so the loop ingests
deterministically with no network.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from flow_core.db import admin_session, tenant_session
from flow_core.email_connector import (
    FetchedMessage,
    OutgoingMessage,
    set_connector_override,
)
from flow_core.models.email import EmailProvider
from flow_core.services import email as svc
from flow_core.services.auth import signup
from flow_worker import email_sync


class _FakeConnector:
    """In-memory IMAP/SMTP seam: the loop only calls ``fetch``."""

    def __init__(self, messages: list[FetchedMessage]) -> None:
        self._messages = messages
        self.sent: list[OutgoingMessage] = []

    async def fetch(self, *, limit: int = 50) -> list[FetchedMessage]:
        return self._messages[:limit]

    async def send(self, message: OutgoingMessage) -> str:  # pragma: no cover - loop only fetches
        self.sent.append(message)
        return "<sent@flow>"


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _msg(pid: str, subject: str) -> FetchedMessage:
    return FetchedMessage(
        provider_message_id=pid,
        from_addr="alice@example.test",
        to_addrs=["me@example.test"],
        received_at=dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC),
        message_id=f"<{pid}@example.test>",
        subject=subject,
        body_text=f"body of {subject}",
    )


@pytest.fixture(autouse=True)
def _fake_connector():
    """Feed the loop's internal ``connector_for`` an in-memory connector,
    then restore the real network seam so other tests are untouched."""
    set_connector_override(
        lambda _account, _secret: _FakeConnector([_msg("1", "Hello"), _msg("2", "World")])
    )
    try:
        yield
    finally:
        set_connector_override(None)


async def _workspace_with_account(org_name: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name=org_name)
    async with tenant_session(str(a.org_id), str(a.user_id)) as s:
        await svc.create_account(
            s,
            org_id=a.org_id,
            actor_id=a.user_id,
            provider=EmailProvider.imap_generic,
            email_address="me@example.test",
            secret="super-secret-pw",
            imap_host="imap.example.test",
            smtp_host="smtp.example.test",
        )
    return a.org_id, a.user_id


async def test_run_once_ingests_workspace_mail() -> None:
    """A workspace with an account ingests its mail on a sweep, run under
    the owner's authority (no API ctx, no per-call connector passed)."""
    org, user = await _workspace_with_account("ESYNC")

    touched = await email_sync.run_once()

    assert touched >= 1  # at least this workspace ingested
    async with tenant_session(str(org), str(user)) as s:
        msgs = await svc.list_messages(s, org_id=org)
    assert {m.provider_message_id for m in msgs} == {"1", "2"}


async def test_run_once_is_idempotent() -> None:
    """A second sweep is a no-op: known provider_message_ids are skipped,
    so re-running the loop does not duplicate messages."""
    org, user = await _workspace_with_account("ESYNC2")

    await email_sync.run_once()
    await email_sync.run_once()

    async with tenant_session(str(org), str(user)) as s:
        msgs = await svc.list_messages(s, org_id=org)
    assert len(msgs) == 2
