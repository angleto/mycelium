"""Organization: radice della tenancy.

Profilo fiscale emittente come JSONB nello scheletro F0 (la
strutturazione tipizzata, ADR-0003, e raffinata nelle fasi fatturazione).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, TimestampMixin, UUIDPKMixin, VersionMixin


class Organization(UUIDPKMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    fiscal_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
