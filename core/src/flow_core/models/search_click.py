"""Append-only search-result click log (ADR-0035 ``recall_at_k``).

One immutable row per "the user ran a search and opened a result":
which query, which entity (kind + id) was clicked, at which 1-based
rank of the ranked result list, out of how many results were shown.
``is_probe`` marks synthetic golden-fixture probes (test corpus runs)
so the recall sensor reads real queries only. RLS-scoped by ``org_id``
(ADR-0007). The clicked entity is NOT an FK: the row is an event that
must survive the entity's deletion (same stance as
``classification_feedback``).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin

# The result kinds the unified /search returns. Mirrored by the CHECK
# constraint in migration 0041 (single source: keep in sync).
SEARCH_CLICK_KINDS: frozenset[str] = frozenset({"task", "note", "blob"})

# Queries longer than this are truncated at write time (a click log is
# telemetry, not a document store).
SEARCH_CLICK_QUERY_MAX = 500


class SearchClick(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "search_clicks"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    query: Mapped[str] = mapped_column(String(SEARCH_CLICK_QUERY_MAX), nullable=False)
    hit_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # The clicked entity's id (task / note / blob id depending on kind).
    hit_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # 1-based position of the clicked hit in the ranked result list.
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    # How many ranked results were shown for the query (the K the user saw).
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_probe: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
