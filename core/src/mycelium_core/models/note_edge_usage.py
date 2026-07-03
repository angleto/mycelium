"""Search-informed per-edge usage counters (Fase 0 of the
search-informed graph, task 561c6aca).

Pair-keyed clone of ``note_coactivity``: one row per canonical
undirected note pair (``note_a_id <= note_b_id`` by string order, the
``graph._pair_key`` rule), covering *unlinked* pairs uniformly -- the
link_add / link_direct candidates are exactly pairs no ``NoteNoteLink``
row can represent. Materialised offline by the Phase-2 aggregation
(``refresh_edge_usage``) from ``retrieval_trace``; the read side will
fold ``decay_score`` into the soft-OR of
``graph.compute_note_edge_weights`` as its fourth input (an empty table
is byte-identical to today, pinned in test).

Direction (``forward_count`` / ``backward_count``, tallied relative to
the canonical order) stays OUT of the undirected weight -- PageRank /
betweenness / Leiden assume undirected -- and only surfaces as proposed
``link_direct`` candidates, never automatic edges.

In Fase 0 the table exists and stays EMPTY: substrate only.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Float, ForeignKey, Integer, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from mycelium_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class NoteEdgeUsage(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "note_edge_usage"
    __table_args__ = (
        UniqueConstraint("org_id", "note_a_id", "note_b_id", name="uq_note_edge_usage_pair"),
    )

    # Canonical undirected pair: ``note_a_id <= note_b_id`` by string
    # order (the rule ``graph._pair_key`` uses), so a pair folds into one
    # row regardless of traversal direction.
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
    # Co-retrieval magnitude inside the rolling window (how often the
    # pair appeared ranking-adjacent in served results).
    traversal_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Directional tallies relative to the canonical (a, b) order:
    # ``forward`` = a ranked above b. Feed the link_direct proposal,
    # never the undirected weight.
    forward_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    backward_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_traversed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Materialised recency-weighted score (sum of 0.5^(age/half_life)
    # over window traversals, same form as ``memory.recompute_tier``),
    # so the read side is one cheap SELECT.
    decay_score: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
