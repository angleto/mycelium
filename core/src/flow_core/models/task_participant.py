"""TaskParticipant: additional identity pinned to an appointment-task
(migration 0095, ADR-0008 addendum). The window (``start_at`` +
``duration_minutes``) is denormalised from the parent task so the
GiST EXCLUDE constraint on ``identity_id`` can enforce no-ubiquity
without a join (index expressions cannot reach joined tables).

A sync trigger keeps the denormalised columns aligned with the parent
task; dropping the task's appointment status (``duration_minutes`` set
to NULL) removes all participants. The assignee is NOT stored here:
``tasks.no_overlap_event_tasks_per_assignee`` already covers them.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, OrgScopedMixin


class TaskParticipant(OrgScopedMixin, Base):
    __tablename__ = "task_participants"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    identity_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    start_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
