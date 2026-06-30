"""Agent execution run: one LLM-agent task driven end-to-end
(docs/adr/0025, P3).

A run is the metered, bounded, killable execution of ONE already
dispatched ``llm_agent`` task: spawn -> work (a bounded tool/step loop
over the pluggable ``LLMProvider`` seam) -> artifact (a Proposal-A work
note linked to the task) -> complete. Governance is structural, not
advisory: a hard tool allowlist, a step cap, the assigned executor's
``credit_budget``, a cooperative cancel flag, and every tool call runs
as the human actor under RLS + the effective-role RBAC choke point, so
the agent can never exceed the human's permissions.

Org-scoped + RLS exactly like the other tenant tables. ``status``,
``steps`` and ``credits_spent`` are the audited run ledger; the run is
under ``VersionMixin`` so the cancel observer re-loads a fresh row each
step (the cancel signal is a normal optimistic write, not a side
channel)."""

from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
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


class AgentRunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    # Stopped by a guardrail (budget exhausted, or a tool outside the
    # allowlist was requested -> HITL). Distinct from ``failed`` (a
    # provider exception) so the SPA / a future approval UI can tell a
    # safety stop from a crash.
    blocked = "blocked"


class AgentRun(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "agent_runs"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The executor the scheduler dispatched the task to (P2). SET NULL:
    # deleting an executor leaves the run history readable.
    executor_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("executors.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        SAEnum(AgentRunStatus, name="agent_run_status", native_enum=True, create_type=False),
        nullable=False,
    )
    steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    credits_spent: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, server_default="0"
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # The produced work note (the Proposal-A artifact linked to the task).
    artifact_note_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # Cooperative kill switch: the loop re-reads the row each step and
    # stops (status=cancelled) when this is set.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # A short stable slug when status=blocked (budget_exhausted |
    # tool_not_allowed); never free prose (docs/adr/0017).
    blocked_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
