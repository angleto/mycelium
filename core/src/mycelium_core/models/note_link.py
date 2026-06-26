"""Typed M:N relations between notes, and between notes and tasks
(docs/adr/0029 D3, D4).

Two tables, same shape:

- ``note_note_link`` carries the typed relations among notes (the
  mycelial 4-verb model: hypha_of, related, supersedes, contradicts;
  ADR-0040, revises ADR-0029). Humus is a node facet, not a kind.
- ``note_task_link`` carries the four bidirectional flow relations
  between notes and tasks (subject, artifact, derived_from,
  promoted_from). Replaces the single ``notes.task_id`` FK from
  Proposal A; the legacy column survives until ADR-0029 P3.

``created_by`` is an Identity (ADR-0028): a user or an ai_assistant
can both author a link, symmetrically. NULL only for the pre-0088
backfilled rows (no Identity reconstructible).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, OrgScopedMixin, UUIDPKMixin

# Closed set, mirrored at the DB level by CHECK constraints (baseline
# 0001 + migration 0022). The mycelial 4-verb model (ADR-0040, revises
# ADR-0029):
#
#   - ``hypha_of``    : A derived / sprouted from B. DIRECTIONAL
#                       (parent = origin, child = derived). The
#                       decomposition pipeline links a distillation to
#                       its source as a ``hypha_of`` (it derived from
#                       it); humus itself is a node facet, not a kind.
#   - ``related``     : A and B are simply connected. UNDIRECTED, so the
#                       service canonicalises the pair (parent < child by
#                       id) and (a, b) == (b, a).
#   - ``supersedes``  : A makes B obsolete. Directional; auto-prunes the
#                       target (child) toward ``dormant`` on creation.
#   - ``contradicts`` : A refutes B as false. Directional; same
#                       auto-prune. "false" vs "old" is the only signal
#                       distinguishing it from supersedes.
#
# Importance (PageRank) is computed undirected over the weighted weave,
# so a child idea can outrank its origin: direction carries meaning,
# never authority.
NOTE_NOTE_LINK_KINDS: frozenset[str] = frozenset(
    {"hypha_of", "related", "supersedes", "contradicts"}
)
# Kinds whose endpoints are unordered: stored with parent < child (by id
# string) so (a, b) and (b, a) collapse to one edge.
NOTE_NOTE_LINK_UNDIRECTED_KINDS: frozenset[str] = frozenset({"related"})
# Kinds that, on creation, decay the target (child) toward ``dormant``:
# a superseded or contradicted idea rots into the deadwood -> humus
# cycle (one-way nudge; manual maturity still overrides).
NOTE_NOTE_LINK_KILLING_KINDS: frozenset[str] = frozenset({"supersedes", "contradicts"})
NOTE_TASK_LINK_KINDS: frozenset[str] = frozenset(
    {"subject", "artifact", "derived_from", "promoted_from"}
)


class NoteNoteLink(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "note_note_link"
    __table_args__ = (
        UniqueConstraint(
            "parent_note_id",
            "child_note_id",
            "kind",
            name="uq_note_note_link",
        ),
        CheckConstraint("parent_note_id <> child_note_id", name="ck_note_note_link_no_self"),
    )

    parent_note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    child_note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NoteTaskLink(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "note_task_link"
    __table_args__ = (UniqueConstraint("note_id", "task_id", "kind", name="uq_note_task_link"),)

    note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
