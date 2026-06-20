"""Email -> memory ingest (task 2a901dee).

Synced *non-bulk* messages become memory blobs on the 'email' channel
when the account opts in: idempotent, opt-in OFF by default, bulk mail
filtered upstream, searchable via memory.retrieve. In-memory connector
seam (ADR-0023), real DB.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select

from flow_core.db import admin_session, tenant_session
from flow_core.email_connector import FetchedMessage, OutgoingMessage
from flow_core.models.email import EmailProvider
from flow_core.models.memory_blob import BlobSource, MemoryBlob
from flow_core.services import email as svc
from flow_core.services import memory as memory_svc
from flow_core.services import taxonomy
from flow_core.services.auth import signup


class FakeConnector:
    def __init__(self, messages: list[FetchedMessage] | None = None) -> None:
        self.messages = messages or []

    async def fetch(self, *, limit: int = 50) -> list[FetchedMessage]:
        return self.messages[:limit]

    async def send(self, message: OutgoingMessage) -> str:  # pragma: no cover - unused
        return "<sent@flow>"


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _msg(
    pid: str, subject: str, *, is_bulk: bool = False, body: str | None = None
) -> FetchedMessage:
    return FetchedMessage(
        provider_message_id=pid,
        from_addr="alice@example.test",
        to_addrs=["me@example.test"],
        received_at=dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC),
        message_id=f"<{pid}@example.test>",
        subject=subject,
        body_text=body if body is not None else f"body of {subject}",
        is_bulk=is_bulk,
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
        # Exercises the _ACCOUNT_UPDATABLE allow-list path for the flag.
        await svc.update_account(
            s,  # type: ignore[arg-type]
            org_id=org,
            actor_id=user,
            account_id=acc.id,
            expected_version=acc.version,
            values={"ingest_to_memory": True},
        )
    return acc


async def _blob_ids_for(s: object, org: uuid.UUID, msg_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await s.execute(  # type: ignore[attr-defined]
        select(BlobSource.blob_id).where(
            BlobSource.org_id == org,
            BlobSource.source_kind == "email_message",
            BlobSource.source_id == str(msg_id),
        )
    )
    return [r[0] for r in rows]


async def _email_blob_count(s: object, org: uuid.UUID) -> int:
    return (
        await s.execute(  # type: ignore[attr-defined]
            select(func.count())
            .select_from(MemoryBlob)
            .where(MemoryBlob.org_id == org, MemoryBlob.namespace == "email")
        )
    ).scalar_one()


async def test_optin_off_ingests_nothing() -> None:
    org, user = await _signup_org()
    conn = FakeConnector([_msg("1", "Hello")])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user, ingest=False)
        r = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        n_blob = await _email_blob_count(s, org)
    assert r.created == 1
    assert r.ingested == 0
    assert n_blob == 0


async def test_optin_on_ingests_and_is_searchable() -> None:
    org, user = await _signup_org()
    conn = FakeConnector(
        [_msg("1", "Quarterly budget review", body="Please review the Q3 budget by Friday.")]
    )
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user, ingest=True)
        r = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msgs = await svc.list_messages(s, org_id=org)
        blob_ids = await _blob_ids_for(s, org, msgs[0].id)
        hits = await memory_svc.retrieve(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query="quarterly budget",
            operation_id="t-email-search",
            channel_key="email",
        )
        hit_blob_ids = {h.blob.id for h in hits}
    assert r.ingested == 1
    assert len(blob_ids) == 1
    # The ingested email is discoverable on the 'email' channel.
    assert blob_ids[0] in hit_blob_ids


async def test_bulk_mail_filtered_even_when_optin_on() -> None:
    org, user = await _signup_org()
    conn = FakeConnector(
        [_msg("1", "Newsletter", is_bulk=True), _msg("2", "Personal note", is_bulk=False)]
    )
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user, ingest=True)
        r = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        by_subject = {m.subject: m for m in await svc.list_messages(s, org_id=org)}
        bulk_blobs = await _blob_ids_for(s, org, by_subject["Newsletter"].id)
        ok_blobs = await _blob_ids_for(s, org, by_subject["Personal note"].id)
    assert r.created == 2
    assert r.ingested == 1
    assert bulk_blobs == []  # bulk mail kept as a row but never embedded
    assert len(ok_blobs) == 1


async def test_ingest_is_idempotent_across_resyncs() -> None:
    org, user = await _signup_org()
    conn = FakeConnector([_msg("1", "Hello")])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user, ingest=True)
        r1 = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        r2 = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msgs = await svc.list_messages(s, org_id=org)
        blob_ids = await _blob_ids_for(s, org, msgs[0].id)
    assert r1.ingested == 1
    assert r2.created == 0 and r2.ingested == 0  # second run a no-op
    assert len(blob_ids) == 1  # exactly one blob, no duplicate


async def test_optin_after_the_fact_backfills_existing_messages() -> None:
    """Enabling ingest on an account that already has synced history
    backfills it on the next sweep (the steer: process everything)."""
    org, user = await _signup_org()
    conn = FakeConnector([_msg("1", "Old mail")])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user, ingest=False)
        r0 = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        assert r0.ingested == 0  # not opted in yet
        await svc.update_account(
            s,
            org_id=org,
            actor_id=user,
            account_id=acc.id,
            expected_version=acc.version,
            values={"ingest_to_memory": True},
        )
        # No new mail this sweep, but the backlog is now ingested.
        r1 = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
    assert r1.created == 0
    assert r1.ingested == 1


async def test_email_channel_is_listed_after_unblock() -> None:
    org, user = await _signup_org()
    async with tenant_session(str(org), str(user)) as s:
        chans = await taxonomy.list_memory_channels(s, org_id=org)
        keys = {c.system_key for c in chans}
    assert "email" in keys


async def test_ingest_failure_is_isolated_and_surfaced(monkeypatch) -> None:
    """An ingest failure must NOT roll back the synced messages (savepoint
    isolation) and must surface on the account, not commit a clean sync."""

    async def _boom(*_a, **_k):
        raise RuntimeError("embedder down")

    monkeypatch.setattr("flow_core.services.email.memory_svc.write_blob", _boom)

    org, user = await _signup_org()
    conn = FakeConnector([_msg("1", "Keeper")])
    async with tenant_session(str(org), str(user)) as s:
        acc = await _account(s, org, user, ingest=True)
        r = await svc.sync_account(s, org_id=org, actor_id=user, account_id=acc.id, connector=conn)
        msgs = await svc.list_messages(s, org_id=org)
        acc2 = await svc.get_account(s, org_id=org, account_id=acc.id)
        blobs = await _blob_ids_for(s, org, msgs[0].id)
    # The message survived the ingest failure (FR-7 isolation).
    assert r.created == 1
    assert r.ingested == 0
    assert len(msgs) == 1
    assert blobs == []  # ingest savepoint rolled back, no blob
    # The failure is recorded, not masked as a clean active sync.
    assert acc2.status.value == "active"
    assert acc2.last_error is not None and "embedder down" in acc2.last_error
