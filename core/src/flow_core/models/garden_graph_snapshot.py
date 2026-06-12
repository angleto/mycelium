"""Materialised graph-analytics snapshot (task d8664631, Phase 2 of
8c0a8f08).

One row per org holding the offline-computed analytics over the note
weave: PageRank centrality, betweenness centrality (the cluster-bridge
detector, too slow for the request path), Leiden communities and the
partition modularity. ``signature`` is the cheap fingerprint of the
graph inputs the worker uses to skip recomputation when nothing
changed; ``computed_at`` lets consumers label staleness.

The live ``/garden/graph`` and ``/garden/clusters`` endpoints still
compute the cheap analytics on demand (fresh-after-edit UX); this row
is the store for the offline-only betweenness today, and the ready
fast path for the day latency/volume require serving everything from
it. RLS-scoped by ``org_id`` (ADR-0007).
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import Float, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class GardenGraphSnapshot(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "garden_graph_snapshot"
    __table_args__ = (UniqueConstraint("org_id", name="uq_garden_graph_snapshot_org"),)

    # Fingerprint of the inputs the analytics derive from (note /
    # link / note-tag counts + latest link timestamp). Equal signature
    # == the stored analytics are still valid.
    signature: Mapped[str] = mapped_column(String(256), nullable=False)
    # ``{note_id: value}`` maps. JSONB so adding an analytic never
    # needs a schema migration (same stance as garden_health_daily).
    centrality: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    betweenness: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    clusters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    modularity: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
