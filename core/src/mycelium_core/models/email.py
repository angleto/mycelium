"""Email connector models (docs/adr/0023, FR-7).

``EmailAccount.secret_encrypted`` holds a Fernet envelope, never
plaintext (ADR-0006). ``EmailMessage`` is idempotent per account via
``(account_id, provider_message_id)``; it may link to a task."""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class EmailProvider(enum.StrEnum):
    gmail = "gmail"
    imap_generic = "imap_generic"
    proton_bridge = "proton_bridge"


class EmailAccountStatus(enum.StrEnum):
    active = "active"
    error = "error"
    disabled = "disabled"


class EmailAccount(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "email_accounts"
    __table_args__ = (UniqueConstraint("org_id", "email_address", name="uq_email_accounts_org_id"),)

    provider: Mapped[EmailProvider] = mapped_column(
        SAEnum(EmailProvider, name="email_provider", native_enum=True, create_type=False),
        nullable=False,
    )
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    imap_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    imap_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[EmailAccountStatus] = mapped_column(
        SAEnum(
            EmailAccountStatus,
            name="email_account_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
        server_default="active",
    )
    last_sync_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-account opt-in: when true, synced (non-bulk) messages are
    # ingested into the 'email' memory channel (task 2a901dee). OFF by
    # default — ingesting third-party PII into searchable memory is an
    # explicit per-account decision, never automatic.
    ingest_to_memory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Per-account opt-in for the autonomous responder (WS-4): when true and
    # the global ``email_responder_enabled`` is set, each new non-bulk
    # message enqueues a draft reply (withheld until a human approves). OFF
    # by default — drafting + later sending on the user's behalf is an
    # explicit per-account decision.
    auto_draft_replies: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )


class EmailMessage(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "provider_message_id",
            name="uq_email_messages_account_id",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("email_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(998), nullable=True)
    from_addr: Mapped[str] = mapped_column(String(320), nullable=False)
    to_addrs: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(String(500), nullable=True)
    received_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Automated / list / bulk mail (List-Id / Precedence / Auto-Submitted),
    # decided at fetch time. The memory-ingest filter skips these (task
    # 2a901dee); the row is still kept (email-to-task etc. stay available).
    is_bulk: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    raw_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linked_task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    # WS-3: a message can also be promoted to a Note (symmetric with
    # ``linked_task_id``). SET NULL so deleting the note leaves the email.
    linked_note_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="SET NULL"),
        nullable=True,
    )


class EmailAccountDefaultTag(Base):
    """Default tags auto-applied to everything ingested from an account
    (memory blobs on the 'email' channel + email->task/note). A flat set of
    any ``TagKind`` (typical use: one client + one project tag per account),
    so material from a per-client / per-project mailbox is born tagged and
    becomes filterable in memory search. Composite-PK association table
    mirroring ``MemoryBlobTag``; deleting the account or the tag cascades
    here. ``org_id`` carries the RLS predicate (no extra FK; same shape as
    ``MemoryBlobTag.org_id``)."""

    __tablename__ = "email_account_default_tags"
    __table_args__ = (
        PrimaryKeyConstraint("account_id", "tag_id", name="pk_email_account_default_tags"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("email_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EmailResponderJob(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    """One queued draft-reply task for the autonomous responder (WS-4),
    mirroring ``TelegramAssistantJob``. Enqueued (per-account opt-in) when a
    new non-bulk message is synced; the worker drafts a reply via a metered
    LLM call and parks it in state ``drafted``. The draft is WITHHELD until a
    human approves it (``approve`` sends in-thread -> ``sent``; ``reject`` ->
    ``rejected``). UNIQUE on ``message_id`` makes enqueue idempotent.
    ``origin_model_id`` records which model wrote the draft (transparency).
    RLS per-org like the rest of the tenant data (enqueue / review run in a
    tenant session; the worker claims per-org)."""

    __tablename__ = "email_responder_jobs"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_email_responder_jobs_message_id"),
        Index(
            "ix_email_responder_jobs_pending",
            "org_id",
            "created_at",
            postgresql_where="status = 'pending'",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'drafted', 'sent', 'rejected', 'failed')",
            name="ck_email_responder_jobs_status",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("email_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    draft_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sent_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
