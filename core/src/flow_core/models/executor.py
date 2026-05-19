"""Executor: a first-class work resource (docs/adr/0025, P1).

An executor is either a **human** (a workspace user, serial
unit-capacity, bound by ``build_work_calendar`` with an explicit
context-switch penalty) or an **llm_agent** (a K-parallel pool with a
credit budget + per-effort-hour rate, off the human timeline, 24/7).

The scheduler resolves a human's working calendar by ``user_id`` via
``build_work_calendar`` (the executor row only carries the switch
penalty); the LLM executor carries the pool size, budget and rate used
for the K-parallel placement and the cost projection. Defaults make a
fresh workspace a no-op vs the pre-P1 behaviour (human switch cost 0,
one default agent at rate 0): seeded lazily + idempotently, like
``ensure_default_memory_channels``.
"""

from __future__ import annotations

import enum
import uuid
from decimal import Decimal

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class ExecutorKind(enum.StrEnum):
    human = "human"
    llm_agent = "llm_agent"


class Executor(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "executors"

    kind: Mapped[ExecutorKind] = mapped_column(
        SAEnum(ExecutorKind, name="executor_kind", native_enum=True, create_type=False),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Human executors bind to a workspace user (its calendar is still
    # resolved via build_work_calendar by user). NULL for llm_agent.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Human switch penalty: minutes lost when consecutive scheduled
    # tasks for the person differ (no penalty before the first task or
    # for a manual pin). 0 = no penalty (the pre-P1 behaviour).
    context_switch_cost_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    # LLM provider/model binding (P2 uses these for capability routing;
    # P1 keeps a single default pool).
    provider: Mapped[str | None] = mapped_column(String(60), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # LLM concurrency cap (humans are implicitly 1).
    max_parallel: Mapped[int] = mapped_column(Integer, nullable=False, server_default="4")
    # LLM credit cap (NULL = unlimited). P1 only PROJECTS/flags; budget
    # enforcement/admission is P2 (never silently drops a task).
    credit_budget: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    # Credits per effort-hour, for cost projection (0 = free).
    credit_rate_per_hour: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, server_default="0"
    )
    enabled: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    # Capabilities this executor advertises (P2 admission control): an
    # llm task is eligible for this agent iff its
    # ``Task.required_capabilities`` are a subset of these tags. ``text[]``
    # (the flat-string-list norm, like users.backup_codes_hash); matching
    # is set containment done in Python, not a DB array operator. Empty =
    # the agent advertises no specific capability (only routes tasks that
    # require none). Humans may also carry tags but are routed by
    # assignee/calendar (P1), not capability.
    capability_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=text("'{}'")
    )
