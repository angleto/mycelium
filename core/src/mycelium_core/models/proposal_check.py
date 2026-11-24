"""A check of a generated note against its sources (migration 0036, ADR-0076).

A check is evidence for the person deciding the proposal, never a decision:
nothing reads it to approve or decline anything. It records the version of
the note it read, so a check of an earlier version is shown as such.
"""

from __future__ import annotations

import datetime
import enum
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class CheckVerdict(enum.StrEnum):
    #: Every claim is supported by the sources.
    supported = "supported"
    #: At least one claim is unsupported, contradicted or unclear.
    issues = "issues"


class ClaimStatus(enum.StrEnum):
    supported = "supported"
    unsupported = "unsupported"
    contradicted = "contradicted"
    unclear = "unclear"


class ProposalCheck(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "proposal_check"
    __table_args__ = (
        CheckConstraint("verdict IN ('supported', 'issues')", name="ck_proposal_check_verdict"),
    )

    note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    note_version: Mapped[int] = mapped_column(Integer, nullable=False)
    checker_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    checker_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``[{"claim", "status", "evidence", "comment"}]``, validated on the way
    #: in by the operation's schema; ``status`` is a ``ClaimStatus``.
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
