"""Append-only read-path retrieval trace (Fase 0 of the search-informed
graph, task 561c6aca).

One row PER SEARCH holding the returned top-m as a JSONB list of
``{"blob_id": ..., "rank": ...}``. Row-per-search (not row-per-item)
because the Phase-2 aggregation (``refresh_edge_usage``) pairs
*ranking-adjacent* items within one search: with the search as the row,
adjacency is array order and the pairing is a linear O(m) pass -- no
grouping key, no window function -- and the write side stays a single
INSERT per search (design risk #6, write amplification on a read-heavy
path).

Content-free by construction: ids + ranks only, never text, so a trace
is inert once its blobs are erased (the aggregation resolves blob->note
and ids that no longer resolve simply drop out). Retention is windowed
deletion by ``(org_id, created_at)`` -- the composite index below.

``is_probe`` is a forward hook: today probe traffic (eval harness)
skips the trace entirely (the stage is not even mounted, see
``memory.retrieve_with_meta``), but flagged-not-skipped is the likely
future for sampled diagnostics; the column avoids a migration then.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import Boolean, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from mycelium_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class RetrievalTrace(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "retrieval_trace"
    __table_args__ = (
        # Windowed offline aggregation + retention both scan
        # org + time-window; the mixin's org-only index stays for
        # metadata parity with the other org-scoped tables.
        Index("ix_retrieval_trace_org_created", "org_id", "created_at"),
    )

    # The returned top-m in rank order:
    # ``[{"blob_id": "<uuid>", "rank": 1}, ...]``.
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    is_probe: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
