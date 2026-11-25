"""Link proposals and their record (ADR-0073 decision 7).

``link_proposal``
    A proposer suggests a note-to-note link nobody wrote. It is born
    ``proposed`` and takes effect only when a person accepts it, which
    creates a typed link of the kind the person picks. ``proposer`` names
    who proposed it (``builtin:link_add``, ``builtin:unlinked_mention``,
    ``identity:<uuid>`` for a proposer calling the API), and every decision
    is attributed to it so its precision can be measured. ``score`` orders
    the queue and is never returned beside the proposal.
``link_rejection``
    A rejected pair, canonical (``note_a_id`` < ``note_b_id`` by string),
    which no proposer may propose again. A person can still link it by hand.
``link_proposal_exposure``
    Each time a proposal is shown to a person: to whom, on which surface, at
    which position. Decisions alone are not a label set a proposer can learn
    from, because they count only what was shown.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from mycelium_core.models.base import Base, OrgScopedMixin, UUIDPKMixin

PROPOSAL_STATUSES: tuple[str, ...] = ("proposed", "accepted", "rejected", "expired")


class LinkProposal(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "link_proposal"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'accepted', 'rejected', 'expired')",
            name="ck_link_proposal_status",
        ),
        CheckConstraint("source_note_id <> target_note_id", name="ck_link_proposal_no_self"),
        Index(
            "uq_link_proposal_open",
            "org_id",
            "proposer",
            "source_note_id",
            "target_note_id",
            unique=True,
            postgresql_where=text("status = 'proposed'"),
        ),
        Index("ix_link_proposal_queue", "org_id", "status", "source_note_id"),
    )

    proposer: Mapped[str] = mapped_column(String(80), nullable=False)
    source_note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    target_note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    # What the proposer offers as its reason: a quote, a weight, a pointer.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # A reference into the proposer's own evidence store, kept on the link
    # if the proposal is accepted (the origin survives acceptance).
    evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Orders the queue; never shown beside the proposal.
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'proposed'")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    decided_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    accepted_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # ADR-0076 phase B (migration 0037): closed by an assistant's decision
    # under the workspace delegation, not by a person. The proposer precision
    # counts a person's judgements only.
    delegated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )


class LinkRejection(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "link_rejection"
    __table_args__ = (
        CheckConstraint("note_a_id < note_b_id", name="ck_link_rejection_canonical"),
        Index("uq_link_rejection_pair", "org_id", "note_a_id", "note_b_id", unique=True),
    )

    note_a_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    note_b_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("link_proposal.id", ondelete="SET NULL"), nullable=True
    )
    rejected_by: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    rejected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class LinkProposalExposure(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "link_proposal_exposure"
    __table_args__ = (Index("ix_link_proposal_exposure_proposal", "proposal_id"),)

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("link_proposal.id", ondelete="CASCADE"), nullable=False
    )
    shown_to: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    surface: Mapped[str] = mapped_column(String(40), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    shown_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
