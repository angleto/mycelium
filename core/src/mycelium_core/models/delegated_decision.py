"""A decision an assistant took on a person's behalf (migration 0037, ADR-0076).

Written only under the workspace setting ``garden_delegation``, which only a
person writes. It names the assistant, so what agents decided can be listed
apart from what the person decided, and it records whether the person
reversed it, which is how often they disagree.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class DelegatedDecision(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "delegated_decision"
    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('synthesis', 'link_proposal', 'fact')",
            name="ck_delegated_decision_target_kind",
        ),
        CheckConstraint("verdict IN ('approve', 'decline')", name="ck_delegated_decision_verdict"),
    )

    #: The ``garden_delegation.DecisionKind`` that allowed it.
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    verdict: Mapped[str] = mapped_column(String(10), nullable=False)
    assistant_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: What the decision created, when it created something: the link an
    #: accepted link proposal became, so reversing it removes that link.
    result_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reversed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reversed_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
