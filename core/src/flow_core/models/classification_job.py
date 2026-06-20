"""On-create classification queue (ADR-0042 D5, task b8c60940 / WS-D2).

``create_note`` / ``create_task`` enqueue one row here in the create's OWN
transaction (a rolled-back create enqueues nothing); the garden worker drains
pending rows -- running ``classify_node`` and caching the result in
``precomputed_suggestions`` (D4) -- so a new node is classified within a tick
of creation instead of waiting for the forester to open the panel. Read-only
proposals (nothing is auto-applied).

``node_id`` is polymorphic (a note OR a task) so it carries no FK (mirrors
``classification_feedback``); ``node_kind`` disambiguates. ``status`` walks
``pending -> done | error`` (a poison node is marked ``error`` with the
message, never retried in a loop). RLS per-org (ADR-0007 / 0025 pattern).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Index, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin

CLASSIFICATION_JOB_STATUSES: frozenset[str] = frozenset({"pending", "done", "error"})


class ClassificationJob(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "classification_jobs"
    __table_args__ = (
        # The worker drains pending oldest-first, per org.
        Index(
            "ix_classification_job_org_status_created",
            "org_id",
            "status",
            "created_at",
        ),
    )

    # 'note' | 'task' -- node_id is polymorphic, so no FK; node_kind disambiguates.
    node_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # 'pending' (queued) -> 'done' (classified + cached) | 'error' (poison).
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    processed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
