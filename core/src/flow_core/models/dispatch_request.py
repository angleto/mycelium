"""Dispatch request: a human-in-the-loop approval gate for an
``llm_agent`` task the resource-aware scheduler admitted (docs/adr/0025,
P5).

The closed loop (``services.dispatch_loop``) recomputes the P1/P2
schedule, then for every admitted ``llm_agent`` task (capability-matched,
within the agent's ``max_parallel`` WIP and credit budget) with no
active agent run and no active dispatch request it creates ONE
``pending`` request. Governance (ADR-0025 §Governance) makes the default
human-in-the-loop: the loop NEVER spends credits / starts a run without
either an explicit per-dispatch ``approve`` or an explicit workspace
opt-in to ``auto`` mode. An ``approved`` request is executed by the
ONLY metered/bounded path -- ``agent_runtime.start_run`` (P3) -- and the
request moves to ``dispatched`` with the produced ``agent_run_id``.

At most ONE ACTIVE (``pending`` | ``approved``) request per task at a
time: the loop reuses the active row instead of creating a duplicate
per tick (idempotent, mirroring P4's at-most-one-active-handoff-per-edge
rule).

Org-scoped + RLS exactly like the other tenant tables. Under
``VersionMixin`` for optimistic concurrency on the approve/deny API
(``expected_version`` like the other privileged mutations)."""

from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class AutonomousDispatch(enum.StrEnum):
    """The per-workspace autonomous-dispatch policy (docs/adr/0025 P5,
    §Governance). Stored in ``organizations.settings.autonomous_dispatch``;
    the default when unset resolves to ``approval_required`` -- the
    governance default is human-in-the-loop, never auto-spend without an
    explicit opt-in."""

    # The loop is disabled: ``tick`` is a no-op (no requests, no runs).
    off = "off"
    # DEFAULT: the loop creates ``pending`` requests; a human must
    # approve each one before any credit is spent / run is started.
    approval_required = "approval_required"
    # The loop auto-approves + dispatches without a manual click. Still
    # bounded by the per-agent WIP, the credit budget, the tool
    # allowlist and the RBAC ceiling -- ``auto`` removes clicks, NOT the
    # guardrails.
    auto = "auto"


# The governance default: human-in-the-loop. An unset / unknown setting
# resolves here, never to ``auto`` (no silent auto-spend).
DEFAULT_AUTONOMOUS_DISPATCH = AutonomousDispatch.approval_required


class DispatchStatus(enum.StrEnum):
    # The loop proposed this dispatch; a human must approve it (the
    # human-in-the-loop default) -- ACTIVE.
    pending = "pending"
    # Approved (by a human via the API, or auto-approved by the loop
    # under the ``auto`` workspace policy). Still ACTIVE: the run has
    # not started yet (the same tick / an inline dispatch starts it).
    approved = "approved"
    # An agent run was started for the task via the P3 metered path;
    # ``agent_run_id`` is set. Terminal for this request.
    dispatched = "dispatched"
    # A human denied it (never starts a run). Terminal.
    denied = "denied"
    # The scheduler no longer admits the task (deps changed, budget
    # exhausted, executor removed, ...) so a still-pending request was
    # retired by the loop. Terminal; a fresh request is created if the
    # task becomes admissible again.
    skipped = "skipped"
    # Starting the run failed (provider error, budget exhausted at run
    # time, blocked, ...). Terminal, non-fatal: it never aborts the
    # tick or other tasks; ``reason`` carries the short cause.
    failed = "failed"


# The two ACTIVE statuses: a task with a row in either state must not
# get a second request (the at-most-one-active invariant, like the P4
# active-handoff set).
ACTIVE_DISPATCH_STATUSES = (DispatchStatus.pending, DispatchStatus.approved)


class DispatchRequest(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "dispatch_requests"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The executor the P2 scheduler dispatched the task to (the run will
    # be started against it). SET NULL: deleting an executor leaves the
    # request history readable.
    executor_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("executors.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[DispatchStatus] = mapped_column(
        SAEnum(DispatchStatus, name="dispatch_status", native_enum=True, create_type=False),
        nullable=False,
    )
    # The scheduler's projected credit cost for THIS task (the row's
    # Schedule.projected_cost): what approving it is expected to spend.
    projected_credit_cost: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, server_default="0"
    )
    # The agent run started when the request was dispatched (P3). SET
    # NULL: deleting a run leaves the request history readable.
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    requested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The user who approved/denied it (NULL for a loop auto-decision /
    # an auto-skip). Not an FK to keep the audit row independent of the
    # users table lifecycle (consistent with other audit-ish ids).
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # A short stable cause for deny/skip/failed (never free prose, like
    # ``agent_runs.blocked_reason``; docs/adr/0017).
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
