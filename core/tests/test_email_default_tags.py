"""Per-account default tags (WS-1).

A flat set of tags (typ. client + project) set on an email account is
auto-applied to everything ingested from it: memory blobs on the 'email'
channel (filterable by those tags) and tasks created via email->task.
In-memory connector seam (ADR-0023), real DB.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.email_connector import FetchedMessage, OutgoingMessage
from mycelium_core.errors import ConflictError, NotFoundError
from mycelium_core.models.email import EmailProvider
from mycelium_core.models.memory_blob import BlobSource, MemoryBlobTag
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.services import email as svc
from mycelium_core.services import memory as memory_svc
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput


class FakeConnector:
    def __init__(self, messages: list[FetchedMessage] | None = None) -> None:
        self.messages = messages or []

    async def fetch(self, *, limit: int = 50) -> list[FetchedMessage]:
        return self.messages[:limit]

    async def send(self, message: OutgoingMessage) -> str:  # pragma: no cover - unused
        return "<sent@mycelium>"


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _msg(pid: str, subject: str, *, body: str | None = None) -> FetchedMessage:
    return FetchedMessage(
        provider_message_id=pid,
        from_addr="alice@example.test",
        to_addrs=["me@example.test"],
        received_at=dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC),
        message_id=f"<{pid}@example.test>",
        subject=subject,
        body_text=body if body is not None else f"body of {subject}",
        is_bulk=False,
    )


async def _signup_org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EM")
    return a.org_id, a.user_id


async def _account(s: object, org: uuid.UUID, user: uuid.UUID, *, ingest: bool):
    acc = await svc.create_account(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        provider=EmailProvider.imap_generic,
        email_address="me@example.test",
        secret="pw",
        imap_host="imap.example.test",
        smtp_host="smtp.example.test",
    )
    if ingest:
        await svc.update_account(
            s,  # type: ignore[arg-type]
            org_id=org,
            actor_id=user,
            account_id=acc.id,
            expected_version=acc.version,
            values={"ingest_to_memory": True},
        )
    # Re-load so the returned object's ``version`` reflects the DB (async
    # SQLAlchemy never lazy-loads an expired attribute, so callers must hold
    # a freshly-loaded row before reading ``.version``).
    return await svc.get_account(s, org_id=org, account_id=acc.id)  # type: ignore[arg-type]


async def _blob_ids_for(s: object, org: uuid.UUID, msg_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await s.execute(  # type: ignore[attr-defined]
        select(BlobSource.blob_id).where(
            BlobSource.org_id == org,
            BlobSource.source_kind == "email_message",
            BlobSource.source_id == str(msg_id),
        )
    )
    return [r[0] for r in rows]


async def _blob_tag_ids(s: object, blob_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await s.execute(  # type: ignore[attr-defined]
        select(MemoryBlobTag.tag_id).where(MemoryBlobTag.blob_id == blob_id)
    )
    return set(rows.scalars().all())


async def _mk_client_project(s, org, user) -> tuple[uuid.UUID, uuid.UUID]:
    """A real client -> project chain. Not ``create_tag``: since
    docs/adr/0003 that door refuses client/project (a bare ``tags`` row
    has no satellite profile, so the project would have no owning
    client and every project -> client lookup on it would fail)."""
    client = await taxonomy.create_client(
        s,
        org_id=org,
        actor_id=user,
        name="Acme",
        profile=ClientInput(legal_name="Acme SRL"),
    )
    project = await taxonomy.create_project(
        s, org_id=org, actor_id=user, name="Website", client_tag_id=client.id
    )
    return client.id, project.id


async def test_default_tags_applied_to_ingested_blob_and_filter_search() -> None:
    org, user = await _signup_org()
    conn = FakeConnector([_msg("1", "Quarterly budget", body="Q3 budget by Friday.")])
    async with tenant_session(str(org), str(user)) as s:
        client_id, project_id = await _mk_client_project(s, org, user)
        acc = await _account(s, org, user, ingest=True)
        await svc.set_default_tags(
            s,
            org_id=org,
            actor_id=user,
            account_id=acc.id,
            expected_version=acc.version,
            tag_ids=[client_id, project_id],
        )
        r = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msgs = await svc.list_messages(s, org_id=org)
        blob_ids = await _blob_ids_for(s, org, msgs[0].id)
        tag_ids = await _blob_tag_ids(s, blob_ids[0])
        # Filtering memory search by the client tag still returns the email.
        hits = await memory_svc.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query="quarterly budget",
            operation_id="t-email-tagfilter",
            channel_key="email",
            tag_ids=[client_id],
        )
        hit_ids = {h.blob.id for h in hits}
    assert r.ingested == 1
    # The blob carries both default tags (plus the 'email' channel tag).
    assert {client_id, project_id} <= tag_ids
    assert blob_ids[0] in hit_ids


async def test_default_tags_applied_on_email_to_task() -> None:
    org, user = await _signup_org()
    conn = FakeConnector([_msg("1", "Invoice question")])
    async with tenant_session(str(org), str(user)) as s:
        client_id, project_id = await _mk_client_project(s, org, user)
        acc = await _account(s, org, user, ingest=False)
        await svc.set_default_tags(
            s,
            org_id=org,
            actor_id=user,
            account_id=acc.id,
            expected_version=acc.version,
            tag_ids=[client_id, project_id],
        )
        await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msg = (await svc.list_messages(s, org_id=org))[0]
        task_id = await svc.email_to_task(s, org_id=org, actor_id=user, message_id=msg.id)
        rows = await s.execute(select(TaskTag.tag_id).where(TaskTag.task_id == task_id))
        task_tag_ids = set(rows.scalars().all())
    assert {client_id, project_id} <= task_tag_ids


async def test_set_default_tags_rejects_unknown_tag() -> None:
    org, user = await _signup_org()
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user, ingest=False)
        with pytest.raises(NotFoundError):
            await svc.set_default_tags(
                s,
                org_id=org,
                actor_id=user,
                account_id=acc.id,
                expected_version=acc.version,
                tag_ids=[uuid.uuid4()],
            )


async def test_set_default_tags_replaces_and_bumps_version() -> None:
    org, user = await _signup_org()
    async with tenant_session(str(org), str(user)) as s:
        client_id, project_id = await _mk_client_project(s, org, user)
        acc = await _account(s, org, user, ingest=False)
        acc_id = acc.id
        base = acc.version
        v1 = await svc.set_default_tags(
            s,
            org_id=org,
            actor_id=user,
            account_id=acc_id,
            expected_version=base,
            tag_ids=[client_id, project_id],
        )
        # Set-replace down to just the client tag.
        v2 = await svc.set_default_tags(
            s,
            org_id=org,
            actor_id=user,
            account_id=acc_id,
            expected_version=v1,
            tag_ids=[client_id],
        )
        by_acc = await svc.default_tags_by_account(s, account_ids=[acc_id])
        default_ids = [t.id for t in by_acc[acc_id]]
        # A stale version is rejected (optimistic lock).
        with pytest.raises(ConflictError):
            await svc.set_default_tags(
                s,
                org_id=org,
                actor_id=user,
                account_id=acc_id,
                expected_version=v1,  # stale
                tag_ids=[project_id],
            )
    assert v1 == base + 1
    assert v2 == v1 + 1
    assert default_ids == [client_id]
