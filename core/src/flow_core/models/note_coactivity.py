"""Materialised note co-activity edge (task f0a15247, ADR-0031 v2+).

The third evidence source behind ``note_edge_strength``: two notes that
are *touched in the same working session* are practically tied even when
no manual link and no shared tag connect them. ADR-0031 calls this the
co-activity weight ``w_coact`` ("a normalised co-task / co-time-slot
count, sourced from Proposal A's activity log").

The activity log is an unbounded append-only stream and
``compute_note_edge_weights`` is on the request path (PageRank,
betweenness, Leiden and the walk all fan out through it), so the pairwise
aggregation is materialised here by the offline worker
(``services/coactivity.refresh_coactivity``) and read back as one cheap
SELECT, mirroring the betweenness/snapshot split (task d8664631).

One row per *undirected* pair, canonicalised ``note_a_id <= note_b_id``
(string order, matching ``services/graph._pair_key``). ``session_count``
is the number of distinct working sessions in which the pair co-occurred;
the read side squashes it into ``[0, 1]``. RLS-scoped by ``org_id``
(ADR-0007); the note FKs cascade so a hard-deleted note drops its edges.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import ForeignKey, Integer, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class NoteCoactivity(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "note_coactivity"
    __table_args__ = (
        UniqueConstraint("org_id", "note_a_id", "note_b_id", name="uq_note_coactivity_pair"),
    )

    # Canonical undirected pair: ``note_a_id <= note_b_id`` by string
    # order (the rule ``graph._pair_key`` uses), so a pair folds into one
    # row regardless of which note was touched first.
    note_a_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
    )
    note_b_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Number of distinct working sessions in which both notes were
    # touched. The read side (``graph.coactivity_weight``) squashes this
    # into a saturating [0, 1] contribution.
    session_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The most recent session timestamp the pair co-occurred at. Part of
    # the snapshot signature (content fingerprint) and a staleness cue.
    last_coactive_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
