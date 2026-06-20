"""Persisted ``classify_node`` suggestions (ADR-0042 D4, task b8c60940 / WS-D2).

The on-create classification job (ADR-0042 D5) writes a node's structured
proposals here so they are ready the instant the forester opens the node,
instead of recomputed live on every open. One row per proposed item:
``suggestion_value`` is the verbatim proposal -- the same shape ``garden_apply``
consumes (``tag_id`` / ``target_id`` + ``link_kind`` / maturity ``value`` /
``leiden_id``); ``confidence`` + ``rationale`` mirror the live ``classify_node``
output; ``computed_at`` drives a freshness TTL (a stale row is recomputed
live, since the graph may have moved under it -- these are read-only
proposals, the human decides).

The table is a CACHE, not a log: a recompute DELETEs a node's rows and
rewrites them. ``node_id`` is polymorphic (a note OR a task) so it carries
no FK (mirrors ``classification_feedback``); ``node_kind`` disambiguates.
RLS per-org (ADR-0007 / 0025 pattern).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Float, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class PrecomputedSuggestion(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "precomputed_suggestions"
    __table_args__ = (
        # The hot path reads a node's cache and checks freshness.
        Index("ix_precomputed_suggestion_org_node", "org_id", "node_id"),
    )

    # 'note' | 'task' -- node_id is polymorphic, so no FK (like
    # classification_feedback); node_kind disambiguates.
    node_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # 'tag' | 'link' | 'maturity' | 'cluster' (classification_feedback's set).
    suggestion_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Verbatim proposal, the shape garden_apply consumes.
    suggestion_value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    rationale: Mapped[str | None] = mapped_column(String, nullable=True)
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
