"""Email service (docs/adr/0023, FR-7).

Account CRUD with the secret stored as a Fernet envelope (never
echoed), idempotent per-account sync (skip known
provider_message_id), per-account fault isolation, email-to-task with
a source link, and reply-in-thread. The connector is injected so the
external IMAP/SMTP boundary is a seam (tests pass a fake).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import String, cast, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.concurrency import optimistic_update
from mycelium_core.config import get_settings
from mycelium_core.crypto import decrypt_secret, encrypt_secret
from mycelium_core.email_connector import (
    EmailConnector,
    OutgoingMessage,
    connector_for,
)
from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.google_api import GoogleApiClient, google_api_client
from mycelium_core.i18n import MessageCode
from mycelium_core.models.email import (
    EmailAccount,
    EmailAccountDefaultTag,
    EmailAccountStatus,
    EmailMessage,
    EmailProvider,
    EmailResponderJob,
)
from mycelium_core.models.membership import Role
from mycelium_core.models.memory_blob import BlobSource
from mycelium_core.models.note import NoteKind
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.services import audit, tag_assignment
from mycelium_core.services import memory as memory_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.rbac import require_role

_log = logging.getLogger("mycelium.email")

_ACCOUNT_UPDATABLE = frozenset(
    {
        "display_name",
        "imap_host",
        "imap_port",
        "smtp_host",
        "smtp_port",
        "status",
        "ingest_to_memory",
        "auto_draft_replies",
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


async def _tag_kinds(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, TagKind]:
    """``id -> kind`` for every requested tag, in one query. RLS scopes
    ``tags`` to the org, so another workspace's tag simply does not come
    back and the whole set is refused with TAG_NOT_FOUND -- a caller
    cannot bind an account to a tag it cannot see."""
    if not ids:
        return {}
    rows = (await session.execute(select(Tag.id, Tag.kind).where(Tag.id.in_(list(ids))))).all()
    kinds: dict[uuid.UUID, TagKind] = {tag_id: kind for tag_id, kind in rows}
    if any(tag_id not in kinds for tag_id in ids):
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    return kinds


async def default_tag_ids(session: AsyncSession, account_id: uuid.UUID) -> set[uuid.UUID]:
    """The tag ids auto-applied to everything ingested from this account."""
    rows = await session.execute(
        select(EmailAccountDefaultTag.tag_id).where(EmailAccountDefaultTag.account_id == account_id)
    )
    return set(rows.scalars().all())


async def default_tags_by_account(
    session: AsyncSession, *, account_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Tag]]:
    """Default ``Tag`` rows per account (single query, ordered by
    kind/name), for serialisation on ``EmailAccountOut``."""
    if not account_ids:
        return {}
    rows = await session.execute(
        select(EmailAccountDefaultTag.account_id, Tag)
        .join(Tag, Tag.id == EmailAccountDefaultTag.tag_id)
        .where(EmailAccountDefaultTag.account_id.in_(list(account_ids)))
        .order_by(Tag.kind, Tag.name)
    )
    out: dict[uuid.UUID, list[Tag]] = {}
    for account_id, tag in rows.all():
        out.setdefault(account_id, []).append(tag)
    return out


async def set_default_tags(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    account_id: uuid.UUID,
    expected_version: int,
    tag_ids: Sequence[uuid.UUID],
) -> int:
    """Replace the account's default-tag set (typ. client + project).
    Validates every tag is visible in the tenant (404 otherwise — an
    explicit set, unlike the silent drop on the ingest path) and that
    the set is a legal structural configuration, then bumps the account
    version (optimistic-lock guard for the SPA)."""
    await require_role(session, org_id, actor_id, Role.member)
    await get_account(session, org_id=org_id, account_id=account_id)
    wanted = list(dict.fromkeys(tag_ids))  # dedupe, preserve order
    kinds = await _tag_kinds(session, wanted)
    if any(kind in (TagKind.client, TagKind.project) for kind in kinds.values()):
        # The structural rule (at most one client, at most one project,
        # and the project's client wins) is checked HERE, at
        # CONFIGURATION time, by the same resolver the ingest paths use.
        # ``email_to_task`` / ``email_to_note`` union this set into
        # ``resolve_structural``, so a mailbox bound to two clients would
        # otherwise only blow up during a later sync, on a message the
        # user cannot connect back to the setting they changed.
        # entity="note" is the permissive shape (project optional): a
        # mailbox bound to a client alone is a legitimate configuration.
        # Guarded by the ``any`` above so a purely free-form default set
        # does not materialise the default client as a side effect.
        await tag_assignment.resolve_structural(
            session, org_id=org_id, actor_id=actor_id, entity="note", requested=wanted
        )
    await session.execute(
        delete(EmailAccountDefaultTag).where(EmailAccountDefaultTag.account_id == account_id)
    )
    for tid in wanted:
        session.add(EmailAccountDefaultTag(account_id=account_id, org_id=org_id, tag_id=tid))
    await session.flush()
    new_version = await optimistic_update(
        session,
        EmailAccount,
        pk=account_id,
        expected_version=expected_version,
        values={},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_account",
        entity_id=account_id,
        action="set_default_tags",
        diff={"tag_ids": ",".join(str(t) for t in wanted)},
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
    # Persist the new messages before ingest queries them.
    await session.flush()
    # Memory ingest (task 2a901dee): per-account opt-in, after the messages
    # are persisted. Wrapped in a SAVEPOINT + broad catch so an ingest
    # failure (a transient DB error, an INSUFFICIENT_CREDITS / RateCard /
    # dim-mismatch DomainError from write_blob's billing/embed path, ...)
    # rolls back ONLY the ingest's own writes and never the synced messages
    # -- neither this account's nor, under the worker's one-session sweep,
    # the other accounts' already-flushed rows (FR-7 per-account isolation).
    # The failure is surfaced on the account (last_error), never masked as a
    # clean sync.
    ingested = 0
    ingest_error: str | None = None
    if account.ingest_to_memory:
        try:
            async with session.begin_nested():
                ingested = await ingest_account_to_memory(
                    session, org_id=org_id, actor_id=actor_id, account_id=account_id
                )
        except Exception as exc:  # savepoint rolled back; isolate from the sync
            ingest_error = str(exc)
            _log.warning("email ingest failed for account=%s: %s", account_id, exc)
    # Autonomous responder (WS-4): enqueue a draft-reply job per new non-bulk
    # message when the account opted in AND the responder is enabled. The
    # worker drafts later; nothing is ever sent without human approval.
    # Best-effort + savepoint-isolated, like the ingest block above.
    if account.auto_draft_replies and get_settings().email_responder_enabled:
        try:
            async with session.begin_nested():
                await enqueue_pending_drafts(
                    session, org_id=org_id, user_id=actor_id, account_id=account_id
                )
        except Exception as exc:  # savepoint rolled back; never blocks the sync
            _log.warning("email draft enqueue failed for account=%s: %s", account_id, exc)
    # Status reflects the OUTCOME: the fetch+persist succeeded (active), and
    # an ingest failure (if any) is recorded in last_error rather than
    # committing a green "no error" row over a silently failed ingest.
    await session.execute(
        update(EmailAccount)
        .where(EmailAccount.id == account_id)
        .values(
            status=EmailAccountStatus.active,
            last_error=ingest_error,
            last_sync_at=dt.datetime.now(tz=dt.UTC),
        )
    )
    await session.flush()
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
    # Per-account default tags (WS-1): every ingested blob is born tagged
    # (typ. client + project), so email memory is filterable by client /
    # project. write_blob merges these with the 'email' channel tag.
    tag_ids = list(await default_tag_ids(session, account_id))
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
            tag_ids=tag_ids,
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


async def get_thread(
    session: AsyncSession, *, org_id: uuid.UUID, thread_id: str
) -> list[EmailMessage]:
    """All messages in a provider thread, oldest first (WS-2). RLS scopes
    to the tenant; ``thread_id`` is the provider's stable conversation id
    (indexed). Lets an agent recall a whole conversation as a unit rather
    than reconstructing it from individual search hits."""
    rows = await session.execute(
        select(EmailMessage)
        .where(EmailMessage.thread_id == thread_id)
        .order_by(EmailMessage.received_at)
    )
    return list(rows.scalars().all())


async def get_thread_for_message(
    session: AsyncSession, *, org_id: uuid.UUID, message_id: uuid.UUID
) -> list[EmailMessage]:
    """The thread containing ``message_id`` (WS-2). Falls back to the lone
    message when the provider gave it no ``thread_id``."""
    msg = await get_message(session, org_id=org_id, message_id=message_id)
    if not msg.thread_id:
        return [msg]
    return await get_thread(session, org_id=org_id, thread_id=msg.thread_id)


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
    description, with a source link back to the message.

    The caller's tags, the explicit ``project_tag_id`` and the account's
    default tags (WS-1) are one bag resolved ONCE: the task inherits the
    mailbox's client + project, and a caller naming a second project (or
    a client the project contradicts) is refused with a stable code
    instead of landing on whichever tag the junction happened to yield.
    """
    await require_role(session, org_id, actor_id, Role.member)
    msg = await get_message(session, org_id=org_id, message_id=message_id)
    structural = await tag_assignment.resolve_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        requested=[*tag_ids, *await default_tag_ids(session, msg.account_id)],
        project_tag_id=project_tag_id,
    )
    # A task always resolves a project (no orphan tasks); the None arm
    # is only there because the dataclass models notes too.
    pair = [structural.client_tag_id]
    if structural.project_tag_id is not None:
        pair.append(structural.project_tag_id)
    task = await tasks_svc.create_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        title=(msg.subject or "(no subject)")[:300],
        description=msg.body_text,
        tag_ids=[*pair, *structural.generic_ids],
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


async def email_to_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    message_id: uuid.UUID,
    tag_ids: Sequence[uuid.UUID] = (),
) -> uuid.UUID:
    """Create a note from a message (WS-3): subject -> title, body -> text,
    with the account's default tags plus any passed tags, and a back-link
    (``linked_note_id``). Symmetric with :func:`email_to_task`.

    The caller's tags and the account's defaults are resolved ONCE. The
    note used to be created first (stamped with the default "Personal"
    client) and then have the mailbox's real client attached on top,
    which GUARANTEED two client tags on every note from a client-bound
    mailbox; the pair below is now the note's whole structural state.
    """
    await require_role(session, org_id, actor_id, Role.member)
    msg = await get_message(session, org_id=org_id, message_id=message_id)
    structural = await tag_assignment.resolve_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        requested=[*tag_ids, *await default_tag_ids(session, msg.account_id)],
    )
    note = await notes_svc.create_note(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind=NoteKind.text,
        title=(msg.subject or "(no subject)")[:300],
        text=msg.body_text,
        # Born on the resolved perimeter, so its blobs never need a
        # re-scope: ``set_structural`` below is a no-op whenever the
        # mailbox carries a project, and is what re-points the client
        # for a mailbox bound to a client alone.
        project_id=structural.project_tag_id,
    )
    await tag_assignment.set_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note.id,
        structural=structural,
    )
    for tag_id in structural.generic_ids:
        # RBAC and existence are the caller's gate (require_role above,
        # note created in this transaction), which is what
        # tag_assignment expects of every door.
        await tag_assignment.attach_generic(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=note.id,
            tag_id=tag_id,
        )
    await optimistic_update(
        session,
        EmailMessage,
        pk=msg.id,
        expected_version=msg.version,
        values={"linked_note_id": note.id},
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_message",
        entity_id=msg.id,
        action="to_note",
        diff={"note_id": str(note.id)},
    )
    return note.id


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


# --- WS-4: autonomous responder (enqueue + human-gated review) ---


async def enqueue_pending_drafts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    cap: int = _INGEST_BATCH_CAP,
) -> int:
    """Queue a draft-reply job for each non-bulk message of the account
    that has no job yet (newest first, capped). Idempotent via the UNIQUE
    on ``message_id``. Gated by the caller (per-account ``auto_draft_replies``
    + global ``email_responder_enabled``). Returns the number enqueued."""
    not_jobbed = ~(
        select(EmailResponderJob.id).where(EmailResponderJob.message_id == EmailMessage.id).exists()
    )
    rows = (
        (
            await session.execute(
                select(EmailMessage.id)
                .where(
                    EmailMessage.account_id == account_id,
                    EmailMessage.is_bulk.is_(False),
                    not_jobbed,
                )
                .order_by(EmailMessage.received_at.desc())
                .limit(cap)
            )
        )
        .scalars()
        .all()
    )
    for mid in rows:
        session.add(
            EmailResponderJob(org_id=org_id, user_id=user_id, message_id=mid, status="pending")
        )
    await session.flush()
    return len(rows)


async def enqueue_draft(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    message_id: uuid.UUID,
) -> uuid.UUID:
    """On-demand: ensure a draft-reply job exists for one message (idempotent
    -- returns the existing job id if already queued). Lets an agent ask the
    responder to draft a specific reply regardless of the per-account flag."""
    await require_role(session, org_id, actor_id, Role.member)
    msg = await get_message(session, org_id=org_id, message_id=message_id)
    existing = (
        await session.execute(
            select(EmailResponderJob).where(EmailResponderJob.message_id == msg.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id
    job = EmailResponderJob(org_id=org_id, user_id=actor_id, message_id=msg.id, status="pending")
    session.add(job)
    await session.flush()
    return job.id


async def get_draft(
    session: AsyncSession, *, org_id: uuid.UUID, job_id: uuid.UUID
) -> EmailResponderJob:
    job = (
        await session.execute(select(EmailResponderJob).where(EmailResponderJob.id == job_id))
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundError(MessageCode.EMAIL_DRAFT_NOT_FOUND)
    return job


async def list_drafts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    statuses: Sequence[str] = ("drafted",),
) -> list[EmailResponderJob]:
    """The review inbox: responder jobs in the given states (default the
    drafted-but-not-sent ones awaiting a human), newest first."""
    rows = await session.execute(
        select(EmailResponderJob)
        .where(EmailResponderJob.status.in_(list(statuses)))
        .order_by(EmailResponderJob.created_at.desc())
    )
    return list(rows.scalars().all())


async def approve_draft(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    job_id: uuid.UUID,
    body_text: str | None = None,
    connector: EmailConnector | None = None,
) -> str:
    """Approve a drafted reply and SEND it in-thread (the only path that ever
    sends a responder draft). ``body_text`` overrides the stored draft so a
    human can edit before sending. Marks the job ``sent``."""
    await require_role(session, org_id, actor_id, Role.member)
    job = await get_draft(session, org_id=org_id, job_id=job_id)
    if job.status != "drafted":
        raise DomainError(MessageCode.EMAIL_DRAFT_NOT_READY)
    body = (body_text if body_text is not None else job.draft_reply) or ""
    if not body.strip():
        raise DomainError(MessageCode.EMAIL_DRAFT_NOT_READY)
    sent_id = await reply_to_message(
        session,
        org_id=org_id,
        actor_id=actor_id,
        message_id=job.message_id,
        body_text=body,
        connector=connector,
    )
    job.status = "sent"
    job.draft_reply = body
    job.sent_id = sent_id
    job.finished_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_responder_job",
        entity_id=job.id,
        action="approve_draft",
        diff={"sent_id": sent_id},
    )
    return sent_id


async def reject_draft(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    job_id: uuid.UUID,
) -> None:
    """Discard a draft (never sent). Idempotent if already rejected."""
    await require_role(session, org_id, actor_id, Role.member)
    job = await get_draft(session, org_id=org_id, job_id=job_id)
    if job.status == "rejected":
        return
    if job.status == "sent":
        raise DomainError(MessageCode.EMAIL_DRAFT_NOT_READY)
    job.status = "rejected"
    job.finished_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="email_responder_job",
        entity_id=job.id,
        action="reject_draft",
    )
