"""Task collaborators (M:N user <-> task). org_id for RLS.

Renamed from ``TaskAssignee`` in migration 0090 (ADR-0028 D2 follow-up):
after the identity-first refactor the singular "assignee" lives on
``tasks.assignee_id``; this table narrowed to "people involved with
the task beyond the assignee", and the new name reflects that
intent.

Service helpers (``assign`` / ``unassign``) keep their public names
for backward compat; under the hood they manipulate this table.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, OrgScopedMixin


class TaskCollaborator(OrgScopedMixin, Base):
    __tablename__ = "task_collaborators"
    __table_args__ = (PrimaryKeyConstraint("task_id", "user_id"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
