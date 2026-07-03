"""Voice/text/conversation notes (docs/adr/0020, 0021, FR-16).

Capture is offline-first and unmetered; processing (STT/LLM/TTS) is
metered. Raw audio is an S3 ref, never stored in the DB. A
``conversation`` note is a thread of ``note_turns``; (org, project)
scope is the hard isolation boundary (ADR-0007)."""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
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


class NoteKind(enum.StrEnum):
    voice = "voice"
    text = "text"
    conversation = "conversation"


class NoteStatus(enum.StrEnum):
    captured = "captured"
    transcribing = "transcribing"
    ready = "ready"
    error = "error"


class NoteMaturity(enum.StrEnum):
    """Garden lifecycle of a note (docs/adr/0029 D2).

    Plants grow, bloom, may wither, may recover. ``seed`` is fresh
    capture; ``growing`` is "I'm touching it"; ``mature`` is the
    user's explicit crystallisation; ``dormant`` is "untouched long
    enough that I might have forgotten about it". Transitions to
    ``dormant`` and from ``seed``/``dormant`` to ``growing`` are
    automatic (a worker tick); ``mature`` is always manual.
    """

    seed = "seed"
    growing = "growing"
    mature = "mature"
    dormant = "dormant"


class TurnRole(enum.StrEnum):
    user = "user"
    assistant = "assistant"


class Note(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "notes"

    # The note's project (and through it, its client) lives in the
    # ``note_tags`` junction as a project-kind tag row, mirroring how
    # ``task_tags`` carries the project for tasks. Migration 0016
    # dropped the legacy ``notes.project_id`` column; query the
    # junction via ``services.notes.project_tag_for_note`` /
    # ``project_tag_ids_for_notes`` to recover it.
    # docs/adr/0029 P3: ``notes.task_id`` (Proposal A's single FK) is
    # gone. The bidirectional note <-> task relation lives in
    # ``note_task_link`` with one of four typed kinds (subject,
    # artifact, derived_from, promoted_from). Use
    # ``services.note_links.primary_task_id_for_note`` /
    # ``primary_task_ids_for_notes`` to recover the canonical task
    # for a note.
    kind: Mapped[NoteKind] = mapped_column(
        SAEnum(NoteKind, name="note_kind", native_enum=True, create_type=False),
        nullable=False,
    )
    status: Mapped[NoteStatus] = mapped_column(
        SAEnum(NoteStatus, name="note_status", native_enum=True, create_type=False),
        nullable=False,
        server_default="captured",
    )
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Phase 6 final (task 1cd8bc0a): ``transcript`` left the Note
    # row in migration 0012. The canonical body now lives in
    # ``note_part`` rows ordered by ``ord``; read via
    # ``services.notes.get_body`` / ``_bodies_by_note``.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audio_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_archived: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    # Fase P (task 561c6aca): finished prose the distiller must never
    # compact. User-set; excludes the note from ``is_inert`` and from
    # every distillation surface (sources and candidates).
    protected: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # docs/adr/0029 D2: garden lifecycle. ``promoted_at`` is set when
    # the note is transplanted to a task (kind='promoted_from'); at
    # that point the service layer treats the note as read-only.
    maturity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="seed")
    promoted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Task 4a718dc4 / ADR-0034: humus = the output of the
    # decomposition pipeline (distillation/pattern/season). ``humus_
    # kind`` carries the subtype; ``humus_flag`` is the read-side
    # eligibility predicate the LLM walk consults.
    humus_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    humus_flag: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    # Idempotency key for synthesised humus (migration 0047, e87daff4):
    # a hash of the source set for a 'pattern' note, "<year>Q<q>" for a
    # 'season' note. NULL for ordinary notes and 1:1 distillations. A
    # partial unique index on (org_id, humus_kind, humus_signature) makes
    # a re-run return the existing synthesis instead of a duplicate.
    humus_signature: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # WS-D2 (ADR-0032 P4): autonomous, read-only classify-on-ingest. The
    # garden sweep stamps a new note with the structural community the
    # offline Leiden snapshot already computed for it (``auto_cluster``)
    # and the time it was first auto-classified (``auto_classified_at``;
    # NULL = not yet seen by the autonomous pass). The opinionated
    # tag/link/maturity suggestions stay human-applied via the live panel.
    auto_cluster: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_classified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ADR-0043 (task e87daff4): human-gated review state for AUTONOMOUSLY-
    # generated nodes. ``origin_model_id`` records the LLM that produced a
    # synthesised note (NULL for human-authored), so the model is on the
    # artifact, not only in the transient MCP response. ``review_state`` is
    # orthogonal to ``maturity``/``humus_flag``: NULL for every human/legacy
    # note AND every user-initiated creation (always effective, unchanged
    # from today); ``'proposed'`` is set ONLY by the autonomous garden sweep
    # when it generates a summary unsolicited (withheld from every retrieval
    # surface); ``'approved'`` once a human accepts it. There is no stored
    # ``'rejected'`` -- a reject soft-deletes the node. A note is EFFECTIVE
    # (eligible for retrieval/listing) iff ``review_state IS DISTINCT FROM
    # 'proposed'`` AND ``deleted_at IS NULL``.
    origin_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_state: Mapped[str | None] = mapped_column(String(16), nullable=True)


class NoteTurn(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "note_turns"
    __table_args__ = (UniqueConstraint("note_id", "ord", name="uq_note_turns_note_id"),)

    note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[TurnRole] = mapped_column(
        SAEnum(TurnRole, name="turn_role", native_enum=True, create_type=False),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
