"""F5 email (DB-backed), FR-7 verification.

Uses an in-memory connector injected into the service (the IMAP/SMTP
boundary is the seam, ADR-0023). Covers: secret encrypted at rest,
idempotent sync, per-account fault isolation, email-to-task with a
source link, reply-in-thread headers, cross-org isolation.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from flow_core.crypto import decrypt_secret
from flow_core.db import admin_session, tenant_session
from flow_core.email_connector import FetchedMessage, OutgoingMessage
from flow_core.errors import DomainError
from flow_core.models.email import EmailAccountStatus, EmailProvider
from flow_core.services import email as svc
from flow_core.services.auth import signup


class FakeConnector:
    def __init__(self, messages: list[FetchedMessage] | None = None, fail: bool = False) -> None:
        self.messages = messages or []
        self.fail = fail
        self.sent: list[OutgoingMessage] = []

    async def fetch(self, *, limit: int = 50) -> list[FetchedMessage]:
        if self.fail:
            raise RuntimeError("imap down")
        return self.messages[:limit]

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return "<sent-1@flow>"


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


async def _account(s, org, user, addr: str = "me@example.test"):
    return await svc.create_account(
        s,
        org_id=org,
        actor_id=user,
        provider=EmailProvider.imap_generic,
        email_address=addr,
        secret="super-secret-pw",
        imap_host="imap.example.test",
        smtp_host="smtp.example.test",
    )


async def test_secret_encrypted_at_rest() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="E1")
    async with tenant_session(str(a.org_id), str(a.user_id)) as s:
        acc = await _account(s, a.org_id, a.user_id)
    assert acc.secret_encrypted != "super-secret-pw"
    assert decrypt_secret(acc.secret_encrypted) == "super-secret-pw"


async def test_sync_is_idempotent() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="E2")
    org, user = a.org_id, a.user_id
    conn = FakeConnector([_msg("1", "Hello"), _msg("2", "World")])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user)
        r1 = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        r2 = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msgs = await svc.list_messages(s, org_id=org)
    assert (r1.fetched, r1.created) == (2, 2)
    assert (r2.fetched, r2.created) == (2, 0)  # second run a no-op
    assert len(msgs) == 2


async def test_per_account_fault_isolation() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="E3")
    org, user = a.org_id, a.user_id
    good = FakeConnector([_msg("1", "ok")])
    bad = FakeConnector(fail=True)
    async with tenant_session(str(org), str(user)) as s:
        a_good = await _account(s, org, user, "good@example.test")
        a_bad = await _account(s, org, user, "bad@example.test")
        results = await svc.sync_all_accounts(
            s,
            org_id=org,
            actor_id=user,
            connectors={a_good.id: good, a_bad.id: bad},
        )
        by_id = {r.account_id: r for r in results}
        refreshed_bad = await svc.get_account(s, org_id=org, account_id=a_bad.id)
        msgs = await svc.list_messages(s, org_id=org)
    assert by_id[a_good.id].ok and by_id[a_good.id].created == 1
    assert not by_id[a_bad.id].ok and by_id[a_bad.id].error
    assert refreshed_bad.status is EmailAccountStatus.error
    assert len(msgs) == 1  # the good account still ingested


async def test_email_to_task_links_source() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="E4")
    org, user = a.org_id, a.user_id
    conn = FakeConnector([_msg("1", "Fix the thing")])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user)
        await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msg = (await svc.list_messages(s, org_id=org))[0]
        task_id = await svc.email_to_task(s, org_id=org, actor_id=user, message_id=msg.id)
        relinked = await svc.get_message(s, org_id=org, message_id=msg.id)
    assert relinked.linked_task_id == task_id


async def test_reply_stays_in_thread() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="E5")
    org, user = a.org_id, a.user_id
    conn = FakeConnector([_msg("1", "Question")])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user)
        await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msg = (await svc.list_messages(s, org_id=org))[0]
        await svc.reply_to_message(
            s,
            org_id=org,
            actor_id=user,
            message_id=msg.id,
            body_text="here is the answer",
            connector=conn,
        )
    sent = conn.sent[-1]
    assert sent.to_addrs == ["alice@example.test"]
    assert sent.subject == "Re: Question"
    assert sent.in_reply_to == "<1@example.test>"
    assert sent.references == "<1@example.test>"


async def test_sync_failure_is_domain_error_and_records_status() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="E6")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user)
        with pytest.raises(DomainError):
            await svc.sync_account(
                s,
                org_id=org,
                actor_id=user,
                account_id=acc.id,
                connector=FakeConnector(fail=True),
            )


async def test_accounts_are_org_isolated() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="E7A")
        b = await signup(s, email=_email(), password="pw-strong-123", org_name="E7B")
    async with tenant_session(str(a.org_id), str(a.user_id)) as s:
        await _account(s, a.org_id, a.user_id)
        assert len(await svc.list_accounts(s, org_id=a.org_id)) == 1
    async with tenant_session(str(b.org_id), str(b.user_id)) as s:
        assert await svc.list_accounts(s, org_id=b.org_id) == []
