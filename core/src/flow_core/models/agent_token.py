"""Agent tokens: long-lived bearer credentials for MCP / external automation.

See migration 0056 for the rationale and the SECURITY DEFINER
``authenticate_agent_token`` helper that lets the verifier find a row
without a tenant GUC.

Storage discipline: the raw value is returned to the operator exactly
once at create time; only its SHA-256 digest (plus a short non-secret
``prefix`` for UI disambiguation) lives in the DB. Lookup at verify
time is O(1) on ``token_hash``.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class AgentToken(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "agent_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Human-readable label ("Claude Desktop", "Cron uploader", ...).
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # First chars of the raw value (e.g. ``flow_at_AbCdEfGh``); not a
    # secret -- shown in the UI so an operator can identify which
    # rotation of a long-lived credential is which.
    prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    # ``sha256(raw_value.encode("utf-8"))``. UNIQUE in the migration.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Capability bucket. ``mcp`` for v1.1; future buckets (e.g.
    # ``webhook``, ``api``) live in the same table without a schema
    # change.
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="mcp")
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["AgentToken"]
