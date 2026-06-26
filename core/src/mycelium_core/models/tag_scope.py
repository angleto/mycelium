"""Tag scoping: restrict a tag to specific projects/clients
(docs/adr/0003). No rows => the tag is global. org_id carried for RLS.
Mirrors NoteTag/TaskTag."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, OrgScopedMixin


class TagScope(OrgScopedMixin, Base):
    __tablename__ = "tag_scopes"
    __table_args__ = (PrimaryKeyConstraint("tag_id", "target_tag_id"),)

    tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False,
    )
    # A project or client tag the tag is scoped to.
    target_tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
