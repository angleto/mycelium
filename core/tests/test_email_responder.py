"""Autonomous email responder (WS-4).

Queue -> draft (metered LLM seam, faked) -> human-gated approve/reject.
A draft is WITHHELD until approved; approve sends in-thread, reject never
sends. Enqueue-on-sync is gated by the per-account flag + the global
``email_responder_enabled``. In-memory connector + fake LLM, real DB.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import func, select

from mycelium_core.ai_providers import LLMResult
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.email_connector import FetchedMessage, OutgoingMessage
from mycelium_core.errors import DomainError
from mycelium_core.models.email import EmailMessage, EmailProvider, EmailResponderJob
from mycelium_core.services import email as svc
from mycelium_core.services import email_responder as responder
from mycelium_core.services.auth import signup


class _FixedLLM:
    model_id = "fake-llm"

    def __init__(self, text: str = "Thanks, that works for me.") -> None:
        self._text = text

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        return LLMResult(text=self._text, tokens_in=1, tokens_out=1, model_id=self.model_id)


class CaptureConnector:
    """Fetches a scripted inbox and records anything sent (for the
    approve/reject assertions)."""

    def __init__(self, messages: list[FetchedMessage] | None = None) -> None:
        self.messages = messages or []
        self.sent: list[OutgoingMessage] = []

    async def fetch(self, *, limit: int = 50) -> list[FetchedMessage]:
        return self.messages[:limit]

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return f"<sent-{len(self.sent)}@mycelium>"


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
        body_text=f"Can we meet about {subject}?",
    )


async def _signup_org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EM")
    return a.org_id, a.user_id


async def _account(s, org, user, *, auto_draft: bool = False):
    acc = await svc.create_account(
        s,
        org_id=org,
        actor_id=user,
        provider=EmailProvider.imap_generic,
        email_address="me@example.test",
        secret="pw",
        imap_host="imap.example.test",
        smtp_host="smtp.example.test",
    )
    if auto_draft:
        await svc.update_account(
            s,
            org_id=org,
            actor_id=user,
            account_id=acc.id,
            expected_version=acc.version,
            values={"auto_draft_replies": True},
        )
    return acc


@pytest.fixture(autouse=True)
def _no_embedder(monkeypatch) -> None:
    """The draft's memory grounding is a best-effort nicety; stub it so the
    responder unit test doesn't load the embedder."""

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr("mycelium_core.services.email_responder.memory_svc.retrieve", _empty)


async def test_claim_and_draft_produces_a_withheld_draft() -> None:
    org, user = await _signup_org()
    conn = CaptureConnector([_msg("1", "the proposal")])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user)
        await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msg = (await svc.list_messages(s, org_id=org))[0]
        job_id = await svc.enqueue_draft(s, org_id=org, actor_id=user, message_id=msg.id)
        ids = await responder.claim_pending(s, limit=10)
        status = await responder.draft_job(
            s, org_id=org, actor_id=user, job_id=job_id, provider=_FixedLLM()
        )
        job = await svc.get_draft(s, org_id=org, job_id=job_id)
    assert job_id in ids
    assert status == "drafted"
    assert job.draft_reply == "Thanks, that works for me."
    assert job.origin_model_id == "fake-llm"
    # Nothing was sent: a draft is withheld until approval.
    assert conn.sent == []


async def test_approve_sends_in_thread_and_reject_does_not() -> None:
    org, user = await _signup_org()
    conn = CaptureConnector([_msg("1", "scheduling")])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user)
        await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msg = (await svc.list_messages(s, org_id=org))[0]
        job_id = await svc.enqueue_draft(s, org_id=org, actor_id=user, message_id=msg.id)
        await responder.claim_pending(s, limit=10)
        await responder.draft_job(s, org_id=org, actor_id=user, job_id=job_id, provider=_FixedLLM())
        # Approve with an edited body -> sends in-thread.
        send_conn = CaptureConnector()
        sent_id = await svc.approve_draft(
            s,
            org_id=org,
            actor_id=user,
            job_id=job_id,
            body_text="Edited reply before sending.",
            connector=send_conn,
        )
        approved = await svc.get_draft(s, org_id=org, job_id=job_id)
        # A second approve on a now-sent draft is refused.
        with pytest.raises(DomainError):
            await svc.approve_draft(
                s, org_id=org, actor_id=user, job_id=job_id, connector=send_conn
            )
    assert sent_id == "<sent-1@mycelium>"
    assert approved.status == "sent"
    assert len(send_conn.sent) == 1
    assert send_conn.sent[0].body_text == "Edited reply before sending."
    # In-thread: the reply references the original Message-ID.
    assert send_conn.sent[0].in_reply_to == msg.message_id


async def test_reject_discards_without_sending() -> None:
    org, user = await _signup_org()
    conn = CaptureConnector([_msg("1", "invoice")])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user)
        await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msg = (await svc.list_messages(s, org_id=org))[0]
        job_id = await svc.enqueue_draft(s, org_id=org, actor_id=user, message_id=msg.id)
        await responder.claim_pending(s, limit=10)
        await responder.draft_job(s, org_id=org, actor_id=user, job_id=job_id, provider=_FixedLLM())
        await svc.reject_draft(s, org_id=org, actor_id=user, job_id=job_id)
        job = await svc.get_draft(s, org_id=org, job_id=job_id)
        drafts = await svc.list_drafts(s, org_id=org)  # default: only 'drafted'
    assert job.status == "rejected"
    assert drafts == []  # a rejected draft drops out of the review inbox


async def test_sync_enqueues_only_when_enabled_and_opted_in() -> None:
    org, user = await _signup_org()
    conn = CaptureConnector([_msg("1", "hello")])
    os.environ["MYCELIUM_EMAIL_RESPONDER_ENABLED"] = "true"
    get_settings.cache_clear()
    try:
        async with tenant_session(str(org), str(user)) as s:
            acc = await _account(s, org, user, auto_draft=True)
            await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
            n = (await s.execute(select(func.count()).select_from(EmailResponderJob))).scalar_one()
        assert n == 1  # opted-in account + responder on -> one job enqueued
    finally:
        del os.environ["MYCELIUM_EMAIL_RESPONDER_ENABLED"]
        get_settings.cache_clear()


async def test_sync_does_not_enqueue_when_account_not_opted_in() -> None:
    org, user = await _signup_org()
    conn = CaptureConnector([_msg("1", "hello")])
    os.environ["MYCELIUM_EMAIL_RESPONDER_ENABLED"] = "true"
    get_settings.cache_clear()
    try:
        async with tenant_session(str(org), str(user)) as s:
            acc = await _account(s, org, user, auto_draft=False)
            await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
            n = (
                await s.execute(
                    select(func.count())
                    .select_from(EmailResponderJob)
                    .join(EmailMessage, EmailMessage.id == EmailResponderJob.message_id)
                    .where(EmailMessage.account_id == acc.id)
                )
            ).scalar_one()
        assert n == 0
    finally:
        del os.environ["MYCELIUM_EMAIL_RESPONDER_ENABLED"]
        get_settings.cache_clear()
