"""Per-issuer-key rate-limit bucket (task 19b7e874, migration 0078).

One row per ``(key_id, endpoint_class)``: a fixed-window counter maintained by an
atomic upsert (see ``mycelium_api.rate_limit``). Accessed via raw SQL for the
single-statement check; the model exists so the ORM mirrors the DB.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base


class IssuerKeyRateLimit(Base):
    __tablename__ = "issuer_key_rate_limit"

    key_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("issuer_api_keys.id", ondelete="CASCADE"),
        primary_key=True,
    )
    endpoint_class: Mapped[str] = mapped_column(String(16), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    window_start: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


__all__ = ["IssuerKeyRateLimit"]
