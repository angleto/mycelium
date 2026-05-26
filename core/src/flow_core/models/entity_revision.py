"""Recovery history for task/note edits (``entity_revision``).

A complete snapshot of an editable entity (task or note) at a point in
time, plus the channel, actor and editing window that produced it.
Polymorphic on ``entity_kind`` so task and note share the same table,
indices, service and UI.

Sealed rows (``sealed_at`` non-NULL) are immutable: a DB trigger
raises on UPDATE. Coalescing into an open row (``sealed_at IS NULL``)
is the only allowed mutation. Restoring produces a NEW row with
``restored_from`` set, never an in-place rewrite.

See migration ``0006_entity_revision`` for the table contract.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class EntityRevision(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "entity_revision"

    # ``task`` | ``note``. CHECK constraint at the DB level keeps the
    # set closed; the polymorphic FK is enforced by the cascade
    # triggers on ``tasks`` and ``notes`` (Postgres has no native
    # polymorphic FK).
    entity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # Full snapshot AFTER the edits of the window. Shape is per-kind;
    # the service module owns the (kind -> field-set) contract.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Union of fields touched within the window. Lets the UI show
    # "title, description" chips without diffing two snapshots.
    changed_fields: Mapped[list[str]] = mapped_column(ARRAY(Text()), nullable=False, default=list)
    # ``web`` is the only coalescing channel; the others write sealed
    # rows on arrival.
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # Client-supplied UUID that ties together the keystrokes of a
    # single editing session (X-Edit-Session-Id header from the SPA).
    # NULL for non-web channels.
    edit_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_from: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version_to: Mapped[int] = mapped_column(BigInteger, nullable=False)
    edit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_edit_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sealed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when this revision is the result of a restore: points at the
    # source revision. The current row stays a normal sealed revision
    # so the timeline shows the restore as a discrete event.
    restored_from: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("entity_revision.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Free-text label users can attach to a revision for a "speaking
    # name" in the history timeline. Populated asynchronously by the
    # worker via the open-model LLM; the user can edit it manually or
    # trigger a regenerate. NULL means "no label yet"; the SPA falls
    # back to the changed_fields list. Decoupled from the snapshot:
    # the immutability trigger has a column allow-list (migration
    # 0010) so summary is the one column that can change on a sealed
    # row.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
