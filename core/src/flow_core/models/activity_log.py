"""Append-only activity log (audit).

No update/delete from code. Append-only is hardened at the DB level by
a policy/trigger in the baseline migration (docs/adr/0002).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class ActivityLog(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "activity_log"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    entity: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
