"""Organization: the tenancy root.

Issuer fiscal profile as JSONB in the F0 skeleton (the typed
structuring, ADR-0003, is refined in the invoicing phases).
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
    # 'active' | 'archived'. Archived workspaces are hidden from the
    # switcher by default but stay fully usable (mirrors tag archive).
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    fiscal_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
