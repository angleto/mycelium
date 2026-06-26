"""Coordination handoff: a typed message bound to a DAG edge
(docs/adr/0025, P4).

When a task reaches a terminal workflow state, for every dependency it
is the predecessor of, a ``TaskHandoff`` is created/refreshed carrying
the producer's artifact (the predecessor's latest agent-run artifact
note, else its latest work note -- may be ``None``: a message-only
handoff is valid) plus a short deterministic system message. It is then
delivered by the successor's RESOLVED executor:

- a **human** executor (or no executor row -> the successor task's
  assignees) gets an in-app ``task_handoff`` notification and the
  artifact note linked to the successor task; status -> ``delivered``.
- an **llm_agent** executor leaves the handoff ``pending``: it is
  consumed by P3 -- ``agent_runtime._build_context`` surfaces pending
  incoming handoffs and ``start_run`` marks them ``consumed``.

Same artifact+message primitive for LLM<->LLM, LLM<->human,
human<->human, human<->LLM. At most one ACTIVE (pending|delivered)
handoff per (predecessor, successor) edge -- re-completion refreshes
the active row rather than duplicating it (enforced in the service).

Org-scoped + RLS exactly like the other tenant tables. ``from/to``
executor FKs are ``ON DELETE SET NULL`` (a handoff outlives an executor
removal -- it is a historical coordination record); the task FKs are
``ON DELETE CASCADE`` (a handoff cannot outlive either of its edge
endpoints).
"""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class HandoffStatus(enum.StrEnum):
    pending = "pending"
    delivered = "delivered"
    consumed = "consumed"
    cancelled = "cancelled"


class TaskHandoff(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "task_handoffs"

    predecessor_task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    successor_task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The producing / consuming executor at handoff time (P2 dispatch
    # result). SET NULL: a handoff is a historical coordination record
    # that survives an executor removal.
    from_executor_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("executors.id", ondelete="SET NULL"),
        nullable=True,
    )
    to_executor_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("executors.id", ondelete="SET NULL"),
        nullable=True,
    )
    message: Mapped[str] = mapped_column(String(1000), nullable=False, server_default="")
    # The producer artifact (the predecessor's agent-run artifact note,
    # else its latest work note). NULL = message-only handoff (valid).
    artifact_note_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    status: Mapped[HandoffStatus] = mapped_column(
        SAEnum(HandoffStatus, name="handoff_status", native_enum=True, create_type=False),
        nullable=False,
        server_default="pending",
    )
    delivered_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ``version`` (bigint) is provided by VersionMixin.
