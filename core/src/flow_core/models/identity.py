"""Identity: the polymorphic addressable entity (docs/adr/0028).

A handle-indexed pointer that wraps either a workspace user
(member of the org) or an AI assistant defined in the org. Lets
``task.assignee_id`` be a single FK column with referential
integrity, instead of a string lookup that branches across two
tables (``users.handle`` vs ``ai_assistants.handle``).

Lifecycle:

- One row per ``(org_id, user_id)`` is created when a user becomes a
  member of the org (signup or invite); dropped on membership
  removal.
- One row per ``ai_assistant`` is created when the assistant is
  defined; dropped on assistant deletion.

The two foreign keys (``user_id`` and ``ai_assistant_id``) are
mutually exclusive: exactly one is non-NULL, enforced by a CHECK
constraint. ``kind`` denormalises the choice so the scheduler /
dispatch can read it without a JOIN.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class IdentityKind(enum.StrEnum):
    user = "user"
    ai_assistant = "ai_assistant"


class Identity(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "identities"

    kind: Mapped[IdentityKind] = mapped_column(
        SAEnum(
            IdentityKind,
            name="identity_kind",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    # Workspace-scoped handle. UNIQUE(org_id, handle) at the DB level
    # (see migration 0084); empty strings are not permitted (the source
    # tables already constrain handles non-empty when used).
    handle: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    ai_assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_assistants.id", ondelete="CASCADE"),
        nullable=True,
    )
