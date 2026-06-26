"""Configurable workflow (docs/adr/0004, FR-6): per-Org definitions
with states and transitions; a project can override the default."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class WorkflowDefinition(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "workflow_defs"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    # Free-form description (added in migration 0065). Read by the
    # SPA's workflow editor and surfaced to MCP agents so they can
    # reason about a task's workflow without inferring from name.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkflowState(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "workflow_states"
    __table_args__ = (UniqueConstraint("workflow_id", "name"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workflow_defs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    ord: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_initial: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    is_terminal: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    # UI hint: hidden columns are skipped in the kanban view by default
    # (the SPA reveals them with a "Show hidden" toggle). Storage-level
    # boolean only — the scheduler / dispatch / transitions don't read it,
    # tasks in a hidden state are still in the workflow normally.
    is_hidden: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    # Free-form description (added in migration 0065). The MCP layer
    # surfaces this on list_tasks / state-info tools so an agent knows
    # what "in_review" or "blocked" means for THIS workflow without
    # guessing from the state name.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkflowTransition(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "workflow_transitions"
    __table_args__ = (UniqueConstraint("workflow_id", "from_state_id", "to_state_id"),)

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workflow_defs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_state_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workflow_states.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_state_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workflow_states.id", ondelete="CASCADE"),
        nullable=False,
    )
