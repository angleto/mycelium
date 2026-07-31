"""Email thread fetch (WS-2) and email -> note (WS-3).

``get_thread`` groups a provider conversation; ``email_to_note`` promotes
a message to a Note with the account's default tags and a back-link.
In-memory connector seam (ADR-0023), real DB.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.email_connector import FetchedMessage, OutgoingMessage
from mycelium_core.models.email import EmailMessage, EmailProvider
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.services import email as svc
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput


class FakeConnector:
    def __init__(self, messages: list[FetchedMessage]) -> None:
        self.messages = messages

    async def fetch(self, *, limit: int = 50) -> list[FetchedMessage]:
        return self.messages[:limit]

    async def send(self, message: OutgoingMessage) -> str:  # pragma: no cover - unused
        return "<sent@mycelium>"


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _msg(
    pid: str,
    subject: str,
    *,
    thread_id: str | None = None,
    minute: int = 0,
    body: str | None = None,
) -> FetchedMessage:
    return FetchedMessage(
        provider_message_id=pid,
        from_addr="alice@example.test",
        to_addrs=["me@example.test"],
        received_at=dt.datetime(2026, 1, 12, 9, minute, tzinfo=dt.UTC),
        thread_id=thread_id,
        message_id=f"<{pid}@example.test>",
        subject=subject,
        body_text=body if body is not None else f"body of {subject}",
    )


async def _signup_org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EM")
    return a.org_id, a.user_id


async def _account(s, org, user):
    return await svc.create_account(
        s,
        org_id=org,
        actor_id=user,
        provider=EmailProvider.imap_generic,
        email_address="me@example.test",
        secret="pw",
        imap_host="imap.example.test",
        smtp_host="smtp.example.test",
    )


async def test_get_thread_groups_by_thread_id_oldest_first() -> None:
    org, user = await _signup_org()
    conn = FakeConnector(
        [
            _msg("a2", "Re: Budget", thread_id="T1", minute=10),
            _msg("a1", "Budget", thread_id="T1", minute=0),
            _msg("b1", "Unrelated", thread_id="T2", minute=5),
        ]
    )
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user)
        await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        thread = await svc.get_thread(s, org_id=org, thread_id="T1")
        pids = [m.provider_message_id for m in thread]
    assert pids == ["a1", "a2"]  # only T1, oldest first


async def test_get_thread_for_message_falls_back_to_lone_message() -> None:
    org, user = await _signup_org()
    conn = FakeConnector([_msg("solo", "No thread", thread_id=None)])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user)
        await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        m = (await svc.list_messages(s, org_id=org))[0]
        thread = await svc.get_thread_for_message(s, org_id=org, message_id=m.id)
    assert [x.id for x in thread] == [m.id]


async def test_email_to_note_links_and_applies_default_tags() -> None:
    org, user = await _signup_org()
    conn = FakeConnector([_msg("1", "Contract draft", body="Please review the contract.")])
    async with tenant_session(str(org), str(user)) as s:
        # ``create_client``, not ``create_tag``: since docs/adr/0003 the
        # plain tag door refuses client/project, because it writes no
        # ``client_profile`` row and a clientless profile breaks every
        # project -> client lookup.
        client = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name="Acme",
            profile=ClientInput(legal_name="Acme SRL"),
        )
        acc = await _account(s, org, user)
        await svc.set_default_tags(
            s,
            org_id=org,
            actor_id=user,
            account_id=acc.id,
            expected_version=acc.version,
            tag_ids=[client.id],
        )
        await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msg = (await svc.list_messages(s, org_id=org))[0]
        note_id = await svc.email_to_note(s, org_id=org, actor_id=user, message_id=msg.id)
        # The message is back-linked to the note.
        linked = (
            await s.execute(select(EmailMessage.linked_note_id).where(EmailMessage.id == msg.id))
        ).scalar_one()
        # The note carries the account's default client tag.
        note_tags = set(
            (await s.execute(select(NoteTag.tag_id).where(NoteTag.note_id == note_id)))
            .scalars()
            .all()
        )
    assert linked == note_id
    assert client.id in note_tags
