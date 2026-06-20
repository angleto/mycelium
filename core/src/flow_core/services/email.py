"""Email service (docs/adr/0023, FR-7).

Account CRUD with the secret stored as a Fernet envelope (never
echoed), idempotent per-account sync (skip known
provider_message_id), per-account fault isolation, email-to-task with
a source link, and reply-in-thread. The connector is injected so the
external IMAP/SMTP boundary is a seam (tests pass a fake).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import String, cast, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.config import get_settings
from flow_core.crypto import decrypt_secret, encrypt_secret
from flow_core.email_connector import (
    EmailConnector,
    OutgoingMessage,
    connector_for,
)
from flow_core.errors import DomainError, NotFoundError
from flow_core.google_api import GoogleApiClient, google_api_client
from flow_core.i18n import MessageCode
from flow_core.models.email import (
    EmailAccount,
    EmailAccountStatus,
    EmailMessage,
    EmailProvider,
)
from flow_core.models.membership import Role
from flow_core.models.memory_blob import BlobSource
from flow_core.services import audit
from flow_core.services import memory as memory_svc
from flow_core.services import tasks as tasks_svc
from flow_core.services.rbac import require_role

_ACCOUNT_UPDATABLE = frozenset(
    {
        "display_name",
        "imap_host",
        "imap_port",
        "smtp_host",
        "smtp_port",
        "status",
        "ingest_to_memory",
    }
)

# Memory-ingest constants (task 2a901dee, ADR-0023 + memory channel).
# The BlobSource (source_kind, source_id) IS the idempotency map
# email_message -> blob, so no separate mapping table is needed.
_INGEST_SOURCE_KIND = "email_message"
# Per-sync ingest cap = natural backpressure: a first opt-in with a large
# backlog drains over several sweeps instead of one burst (the steer is
# "process everything without ever clogging"). New mail is newest-first so
# it is never starved by the backlog.
_INGEST_BATCH_CAP = 50
# Bound the embedded text per message (cost + the embedder's window).
_INGEST_BODY_MAX = 8000


@dataclass(frozen=True)
class SyncResult:
    account_id: uuid.UUID
    fetched: int
    created: int
    ok: bool
    error: str | None = None
    ingested: int = 0


async def get_account(
    session: AsyncSession, *, org_id: uuid.UUID, account_id: uuid.UUID
) -> EmailAccount:
    a = (
        await session.execute(select(EmailAccount).where(EmailAccount.id == account_id))
    ).scalar_one_or_none()
    if a is None:
        raise NotFoundError(MessageCode.EMAIL_ACCOUNT_NOT_FOUND)
    return a


async def list_accounts(session: AsyncSession, *, org_id: uuid.UUID) -> list[EmailAccount]:
    return list(
        (await session.execute(select(EmailAccount).order_by(EmailAccount.email_address)))
        .scalars()
        .all()
    )


async def create_account(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    provider: EmailProvider,
    email_address: str,
    secret: str,
    display_name: str | None = None,
    imap_host: str | None = None,
    imap_port: int | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
) -> EmailAccount:
    await require_role(session, org_id, actor_id, Role.member)
    account = EmailAccount(
        org_id=org_id,
        provider=provider,
        email_address=email_address,
        display_name=display_name,
        imap_host=imap_host,
        imap_port=imap_port,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        secret_encrypted=encrypt_secret(secret),
        status=EmailAccountStatus.active,
    )
    try:
        async with session.begin_nested():
            session.add(account)
            await session.flush()
    except IntegrityError as exc:
        raise DomainError(MessageCode.EMAIL_ACCOUNT_DUPLICATE) from exc
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_account",
        entity_id=account.id,
        action="create",
    )
    return account


async def update_account(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    account_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> int:
    if not values or set(values) - _ACCOUNT_UPDATABLE:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    await get_account(session, org_id=org_id, account_id=account_id)
    new_version = await optimistic_update(
        session,
        EmailAccount,
        pk=account_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_account",
        entity_id=account_id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


async def set_secret(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    account_id: uuid.UUID,
    expected_version: int,
    secret: str,
) -> int:
    """Rotate the opaque secret. Stored encrypted; never logged."""
    await require_role(session, org_id, actor_id, Role.member)
    await get_account(session, org_id=org_id, account_id=account_id)
    new_version = await optimistic_update(
        session,
        EmailAccount,
        pk=account_id,
        expected_version=expected_version,
        values={"secret_encrypted": encrypt_secret(secret)},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_account",
        entity_id=account_id,
        action="set_secret",
    )
    return new_version


async def delete_account(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    a = await get_account(session, org_id=org_id, account_id=account_id)
    await session.delete(a)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_account",
        entity_id=account_id,
        action="delete",
    )


async def access_token_for(
    account: EmailAccount,
    *,
    google_api: GoogleApiClient | None = None,
) -> str:
    """Return a usable secret for the connector. For provider=gmail the
    stored ``secret_encrypted`` is a refresh token: exchange it at
    Google's token endpoint for a short-lived access token (XOAUTH2 uses
    the access token, not the refresh token). For non-gmail providers it
    is the IMAP password (or app password) and is returned as-is.

    The Google HTTP boundary is a Protocol seam (``GoogleApiClient``) so
    tests inject a fake; prod uses the real REST client."""
    plaintext = decrypt_secret(account.secret_encrypted)
    if account.provider is not EmailProvider.gmail:
        return plaintext
    s = get_settings()
    if not s.google_configured:
        raise DomainError(MessageCode.OAUTH_NOT_CONFIGURED)
    api = google_api or google_api_client()
    try:
        token = await api.refresh_access_token(
            refresh_token=plaintext,
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
        )
    except Exception as exc:
        raise DomainError(MessageCode.OAUTH_REFRESH_FAILED, detail=str(exc)) from exc
    return token.access_token


async def _resolve_connector(
    account: EmailAccount,
    connector: EmailConnector | None,
    *,
    google_api: GoogleApiClient | None = None,
) -> EmailConnector:
    if connector is not None:
        return connector
    secret = await access_token_for(account, google_api=google_api)
    return connector_for(account, secret)


async def sync_account(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    account_id: uuid.UUID,
    connector: EmailConnector | None = None,
    limit: int = 50,
) -> SyncResult:
    """Idempotent: known provider_message_id is skipped. Connector
    failure records status/last_error and raises (the caller asked for
    this one account); use :func:`sync_all_accounts` for fault-isolated
    multi-account sync."""
    await require_role(session, org_id, actor_id, Role.member)
    account = await get_account(session, org_id=org_id, account_id=account_id)
    conn = await _resolve_connector(account, connector)
    try:
        fetched = await conn.fetch(limit=limit)
    except Exception as exc:
        await session.execute(
            update(EmailAccount)
            .where(EmailAccount.id == account_id)
            .values(status=EmailAccountStatus.error, last_error=str(exc))
        )
        raise DomainError(MessageCode.EMAIL_SYNC_FAILED, detail=str(exc)) from exc

    known = set(
        (
            await session.execute(
                select(EmailMessage.provider_message_id).where(
                    EmailMessage.account_id == account_id
                )
            )
        )
        .scalars()
        .all()
    )
    created = 0
    for m in fetched:
        if m.provider_message_id in known:
            continue
        session.add(
            EmailMessage(
                org_id=org_id,
                account_id=account_id,
                provider_message_id=m.provider_message_id,
                thread_id=m.thread_id,
                message_id=m.message_id,
                in_reply_to=m.in_reply_to,
                from_addr=m.from_addr,
                to_addrs=", ".join(m.to_addrs),
                subject=m.subject,
                body_text=m.body_text,
                snippet=(m.body_text or "")[:500] or None,
                received_at=m.received_at,
                is_bulk=m.is_bulk,
                raw_size=m.raw_size,
            )
        )
        known.add(m.provider_message_id)
        created += 1
    await session.execute(
        update(EmailAccount)
        .where(EmailAccount.id == account_id)
        .values(
            status=EmailAccountStatus.active,
            last_error=None,
            last_sync_at=dt.datetime.now(tz=dt.UTC),
        )
    )
    await session.flush()
    # Memory ingest (task 2a901dee): per-account opt-in, after the messages
    # are persisted. Own concern, gated on the account flag; idempotent and
    # capped so it never clogs.
    ingested = 0
    if account.ingest_to_memory:
        ingested = await ingest_account_to_memory(
            session, org_id=org_id, actor_id=actor_id, account_id=account_id
        )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_account",
        entity_id=account_id,
        action="sync",
        diff={"fetched": str(len(fetched)), "created": str(created), "ingested": str(ingested)},
    )
    return SyncResult(
        account_id=account_id,
        fetched=len(fetched),
        created=created,
        ok=True,
        ingested=ingested,
    )


def _ingest_body(m: EmailMessage) -> str:
    """The text materialised into the memory blob: a normalised header
    preamble (searchable) + the body, truncated. ``from_addr`` is NOT NULL
    so this is never empty even for a bodyless message."""
    lines = [
        f"Subject: {m.subject}" if m.subject else "Subject: (no subject)",
        f"From: {m.from_addr}",
    ]
    if m.to_addrs:
        lines.append(f"To: {m.to_addrs}")
    lines.append("")
    lines.append((m.body_text or "").strip())
    return "\n".join(lines).strip()[:_INGEST_BODY_MAX]


async def ingest_account_to_memory(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    account_id: uuid.UUID,
    cap: int = _INGEST_BATCH_CAP,
) -> int:
    """Materialise the account's non-bulk, not-yet-ingested messages as
    memory blobs on the 'email' channel (newest first, capped). Idempotent
    via the BlobSource natural key, so a re-sync never duplicates a blob
    and a first opt-in backfills the history a batch per sweep. Returns the
    number of messages ingested this call."""
    not_ingested = ~(
        select(BlobSource.blob_id)
        .where(
            BlobSource.org_id == org_id,
            BlobSource.source_kind == _INGEST_SOURCE_KIND,
            BlobSource.source_id == cast(EmailMessage.id, String),
        )
        .exists()
    )
    rows = (
        (
            await session.execute(
                select(EmailMessage)
                .where(
                    EmailMessage.account_id == account_id,
                    EmailMessage.is_bulk.is_(False),
                    not_ingested,
                )
                .order_by(EmailMessage.received_at.desc())
                .limit(cap)
            )
        )
        .scalars()
        .all()
    )
    for m in rows:
        await memory_svc.write_blob(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=None,
            text_body=_ingest_body(m),
            operation_id=f"email-ingest:{m.id}",
            namespace="email",
            channel_key="email",
            sources=[(_INGEST_SOURCE_KIND, str(m.id))],
        )
    return len(rows)


async def sync_all_accounts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    connectors: dict[uuid.UUID, EmailConnector] | None = None,
    limit: int = 50,
) -> list[SyncResult]:
    """Per-account fault isolation: one account's failure never aborts
    the others (FR-7)."""
    results: list[SyncResult] = []
    for account in await list_accounts(session, org_id=org_id):
        if account.status is EmailAccountStatus.disabled:
            continue
        try:
            results.append(
                await sync_account(
                    session,
                    org_id=org_id,
                    actor_id=actor_id,
                    account_id=account.id,
                    connector=(connectors or {}).get(account.id),
                    limit=limit,
                )
            )
        except DomainError as exc:
            results.append(
                SyncResult(
                    account_id=account.id,
                    fetched=0,
                    created=0,
                    ok=False,
                    error=str(exc),
                )
            )
    return results


async def list_messages(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    linked: bool | None = None,
) -> list[EmailMessage]:
    stmt = select(EmailMessage)
    if account_id is not None:
        stmt = stmt.where(EmailMessage.account_id == account_id)
    if linked is True:
        stmt = stmt.where(EmailMessage.linked_task_id.is_not(None))
    elif linked is False:
        stmt = stmt.where(EmailMessage.linked_task_id.is_(None))
    stmt = stmt.order_by(EmailMessage.received_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_message(
    session: AsyncSession, *, org_id: uuid.UUID, message_id: uuid.UUID
) -> EmailMessage:
    m = (
        await session.execute(select(EmailMessage).where(EmailMessage.id == message_id))
    ).scalar_one_or_none()
    if m is None:
        raise NotFoundError(MessageCode.EMAIL_MESSAGE_NOT_FOUND)
    return m


async def email_to_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    message_id: uuid.UUID,
    project_tag_id: uuid.UUID | None = None,
    tag_ids: Sequence[uuid.UUID] = (),
    assignee_ids: Sequence[uuid.UUID] = (),
) -> uuid.UUID:
    """Create a task from a message (FR-7): subject -> title, body ->
    description, with a source link back to the message."""
    msg = await get_message(session, org_id=org_id, message_id=message_id)
    all_tags = list(tag_ids)
    if project_tag_id is not None and project_tag_id not in all_tags:
        all_tags.append(project_tag_id)
    task = await tasks_svc.create_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        title=(msg.subject or "(no subject)")[:300],
        description=msg.body_text,
        tag_ids=all_tags,
        assignee_ids=list(assignee_ids),
    )
    await optimistic_update(
        session,
        EmailMessage,
        pk=msg.id,
        expected_version=msg.version,
        values={"linked_task_id": task.id},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_message",
        entity_id=msg.id,
        action="to_task",
        diff={"task_id": str(task.id)},
    )
    return task.id


async def send_message(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    account_id: uuid.UUID,
    to_addrs: Sequence[str],
    subject: str,
    body_text: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    connector: EmailConnector | None = None,
) -> str:
    await require_role(session, org_id, actor_id, Role.member)
    account = await get_account(session, org_id=org_id, account_id=account_id)
    conn = await _resolve_connector(account, connector)
    sent_id = await conn.send(
        OutgoingMessage(
            to_addrs=list(to_addrs),
            subject=subject,
            body_text=body_text,
            in_reply_to=in_reply_to,
            references=references,
        )
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_account",
        entity_id=account_id,
        action="send",
    )
    return sent_id


async def reply_to_message(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    message_id: uuid.UUID,
    body_text: str,
    connector: EmailConnector | None = None,
) -> str:
    """Reply staying in the thread (In-Reply-To/References, FR-7)."""
    msg = await get_message(session, org_id=org_id, message_id=message_id)
    subject = msg.subject or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}".strip()
    return await send_message(
        session,
        org_id=org_id,
        actor_id=actor_id,
        account_id=msg.account_id,
        to_addrs=[msg.from_addr],
        subject=subject,
        body_text=body_text,
        in_reply_to=msg.message_id,
        references=msg.message_id,
        connector=connector,
    )
