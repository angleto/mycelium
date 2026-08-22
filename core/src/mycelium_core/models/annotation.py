"""Inline annotations on markdown documents: comments and suggestions.

An annotation is a capability of the *markdown editing surface*, not a
property of a single entity. It attaches to a **markdown document**
addressed by the generic ``(doc_kind, doc_id)`` handle the service
layer exposes, so the same comment/suggestion machinery works on a
note-part body, a task description, and any future markdown field, and
is readable from web/CLI/MCP alike (only the *inline rendering* is
web-specific; the data is part of the document model everywhere).

At the storage level that handle is materialised as **typed nullable
FKs** (``task_id`` / ``note_part_id``) guarded by an XOR CHECK on
``doc_kind``, so ``ON DELETE CASCADE`` referential integrity survives
(a bare polymorphic ``doc_id`` would lose it). The service translates
the boundary ``(doc_kind, doc_id)`` to/from these columns:

- ``doc_kind='task_description'`` -> ``doc_id`` is ``task_id``
- ``doc_kind='note_part'``        -> ``doc_id`` is ``note_part_id``

The table keeps the name ``comments`` (it generalises the former
task-only comments table in place, migration 0023) the same way
migration 0020 kept ``task_checklist_items`` "for migration safety and
to avoid churn"; the ORM class is ``Annotation``.

``kind`` separates a plain ``comment`` (coordination; on a task the
chronological *general* comments -- ``anchor_quote IS NULL`` -- are the
work diary) from a ``suggestion`` (a proposed edit: struck
``original_text`` + ``proposed_text`` + rationale in ``body``, accepted
or rejected in place, never touching the stored markdown until
accepted).

Authorship is an Identity (ADR-0028): a human or an ai_assistant can
author and resolve symmetrically, so an llm_agent reviewer/executor
participates like a person. The legacy task-only ``user_id`` column is
gone (migration 0023 backfills ``author_identity_id`` from it, then
drops it).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)

# Closed sets, mirrored at the DB level by CHECK constraints in
# migration 0023. ``doc_kind`` is extensible: a new markdown field
# adds a value here, a value to the CHECK, and a typed FK column.
ANNOTATION_DOC_KINDS: frozenset[str] = frozenset({"note_part", "task_description"})
ANNOTATION_KINDS: frozenset[str] = frozenset({"comment", "suggestion"})
ANNOTATION_STATUSES: frozenset[str] = frozenset({"open", "resolved", "accepted", "rejected"})


class Annotation(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "comments"
    __table_args__ = (
        CheckConstraint(
            "(doc_kind = 'task_description' AND task_id IS NOT NULL AND note_part_id IS NULL) "
            "OR (doc_kind = 'note_part' AND note_part_id IS NOT NULL AND task_id IS NULL)",
            name="ck_comments_doc_xor",
        ),
        CheckConstraint("kind IN ('comment', 'suggestion')", name="ck_comments_kind"),
        CheckConstraint(
            "status IN ('open', 'resolved', 'accepted', 'rejected')",
            name="ck_comments_status",
        ),
        CheckConstraint(
            "doc_kind IN ('note_part', 'task_description')",
            name="ck_comments_doc_kind",
        ),
    )

    # --- the markdown-document handle (typed FKs + discriminator) ----
    doc_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    note_part_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("note_part.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # --- kind + body -------------------------------------------------
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="comment")
    # Comment text, or a suggestion's rationale. NOT NULL (server_default
    # ''): a suggestion with no rationale stores an empty string.
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    # --- anchor (W3C TextQuoteSelector); NULL quote = whole-doc /
    #     diary entry. Offsets are deliberately NOT stored: the live
    #     body is re-anchored by text search (the editor uses
    #     ProseMirror positions in-session). -----------------------
    # Which DOMAIN the anchor triple below is expressed in.
    #
    # 'source'   the quote is markdown source, located with str.find on the
    #            body. The editor's document is the markdown, so this is what
    #            every anchor captured since that change is.
    # 'rendered' the quote is the editor's RENDERED text (markdown stripped,
    #            links reduced to their label, blocks joined by a space),
    #            resolved through md_anchor's source map. Only rows the
    #            migration could not convert are left here: a rendered anchor
    #            that no longer resolves is already un-paintable, and marking
    #            it keeps the fact that it was not converted rather than
    #            silently re-reading it in the wrong domain.
    anchor_domain: Mapped[str] = mapped_column(String(16), nullable=False, server_default="source")
    anchor_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    anchor_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    anchor_suffix: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- suggestion payload (kind='suggestion') ----------------------
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- lifecycle ---------------------------------------------------
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("comments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Editable/deletable (decision): an author edit stamps ``edited_at``;
    # delete is soft (``deleted_at``); a pending suggestion is withdrawn
    # the same way.
    edited_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- authorship (Identity; ADR-0028) -----------------------------
    author_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_by_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    # --- assignment (task 861b360b / 1f161485 #1) --------------------
    # The person responsible for acting on this annotation (Google-Docs
    # "assign to @someone"). NULL = unassigned. ``SET NULL`` on identity
    # delete (like author/resolved_by); indexed for the "assigned to me"
    # inbox. Assigning is coordination (member role), not authorship.
    assigned_to_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class AnnotationUIState(Base):
    """Per-user presentation state for an annotation card (migration 0084).

    Mirrors ``NotePartUIState``: no row = expanded (the default), a row is
    materialised lazily on the first toggle, and the composite PK keeps one
    state per (user, annotation). Deliberately no ``org_id`` column: the
    ``annotation_id`` FK already pins the row to an org-scoped comment, and
    the RLS policy joins through it (see migration 0084).
    """

    __tablename__ = "annotation_ui_state"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "annotation_id", name="pk_annotation_ui_state"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    annotation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("comments.id", ondelete="CASCADE"), nullable=False
    )
    collapsed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
