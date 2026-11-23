"""References written in bodies, as a derived projection (ADR-0073).

``body_reference_source``
    One row per body that has been projected: a note part or a task
    description, with the SHA-256 of the exact text the rows were computed
    from. A body with no reference still has its row, so "projected and
    empty" and "never projected" are different facts, and the drift
    verifier can tell a body that changed without being re-projected from
    one that simply cites nothing.

``body_reference``
    One row per occurrence of a reference in a source: the token as
    written, its syntax and its ordinal among equal tokens, the target and
    the status of its resolution, and a W3C text-quote anchor in the
    rendered domain.

Nothing writes these tables except the projection in
``services.body_references``; every row is recomputed from the body, and
dropping both loses nothing but the choices people made on ambiguous
occurrences (``chosen_by``), which is why those are the one thing a
recomputation carries over.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from mycelium_core.models.base import Base, OrgScopedMixin, UUIDPKMixin

REFERENCE_SYNTAXES: tuple[str, ...] = ("mention", "chip", "bare")
REFERENCE_TARGET_KINDS: tuple[str, ...] = ("note", "task", "tag")
REFERENCE_STATUSES: tuple[str, ...] = ("resolved", "ambiguous", "dangling")


class BodyReferenceSource(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "body_reference_source"
    __table_args__ = (
        CheckConstraint(
            "(note_part_id IS NULL) <> (task_id IS NULL)",
            name="ck_body_reference_source_one_source",
        ),
        Index(
            "uq_body_reference_source_part",
            "note_part_id",
            unique=True,
            postgresql_where=text("note_part_id IS NOT NULL"),
        ),
        Index(
            "uq_body_reference_source_task",
            "task_id",
            unique=True,
            postgresql_where=text("task_id IS NOT NULL"),
        ),
    )

    note_part_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("note_part.id", ondelete="CASCADE"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BodyReference(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "body_reference"
    __table_args__ = (
        CheckConstraint("syntax IN ('mention', 'chip', 'bare')", name="ck_body_reference_syntax"),
        CheckConstraint(
            "target_kind IS NULL OR target_kind IN ('note', 'task', 'tag')",
            name="ck_body_reference_target_kind",
        ),
        CheckConstraint(
            "status IN ('resolved', 'ambiguous', 'dangling')", name="ck_body_reference_status"
        ),
        CheckConstraint(
            "(status = 'resolved') = (target_id IS NOT NULL AND target_kind IS NOT NULL)",
            name="ck_body_reference_resolved_has_target",
        ),
        Index(
            "uq_body_reference_occurrence",
            "source_id",
            "syntax",
            "token",
            "ordinal",
            unique=True,
        ),
        Index("ix_body_reference_target", "org_id", "target_id"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("body_reference_source.id", ondelete="CASCADE"),
        nullable=False,
    )
    syntax: Mapped[str] = mapped_column(String(8), nullable=False)
    token: Mapped[str] = mapped_column(String(36), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    target_kind: Mapped[str | None] = mapped_column(String(8), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    # A person's choice on an occurrence that was ambiguous when written
    # (ADR-0073 decision 3). Carried over by every recomputation while the
    # token is unchanged; never set by anything else.
    chosen_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    chosen_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quote_exact: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote_suffix: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The commit that first held the occurrence. NULL for occurrences found
    # by the backfill or by the drift verifier: when they were written is
    # not known, and a guess would be a fact nobody recorded.
    first_seen_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
