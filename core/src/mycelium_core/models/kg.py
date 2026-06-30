"""Temporal knowledge graph: typed entities + bi-temporal relation facts.

ADR-0044. ``kg_entity`` nodes are resolved/deduped per (org, type,
normalized name); ``kg_edge`` facts carry a BI-TEMPORAL validity:

- valid-time (the world): ``valid_from`` / ``valid_to`` -- when the fact is
  true in reality (NULL = unbounded on that side).
- transaction-time (the system's belief): ``created_at`` (when asserted) /
  ``invalidated_at`` (when the system stopped believing it; NULL = currently
  believed).

A contradiction INVALIDATES the prior fact (sets ``invalidated_at`` +
``valid_to`` + ``superseded_by_edge_id``) instead of deleting it, so history
stays as-of-queryable -- invalidate-not-delete, mirroring
``entity_revision``'s seal-don't-rewrite. A DB trigger freezes a row once
``invalidated_at`` is set (only the live row may transition).

Facts are born ``review_state='proposed'`` when extracted autonomously and
withheld from every read until adjudicated, exactly like notes (ADR-0043).
The effective-fact predicate is
``invalidated_at IS NULL AND review_state IS DISTINCT FROM 'proposed'`` plus
the as-of window ``(valid_from IS NULL OR valid_from <= :t) AND (valid_to IS
NULL OR valid_to > :t)``.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)

# Closed, enum-like entity taxonomy (the type axis). Predicates, by contrast,
# are an OPEN normalized vocabulary (a KG relation space is not enum-like), so
# ``kg_edge.predicate`` carries NO DB CHECK -- only service-layer normalization.
KG_ENTITY_TYPES: frozenset[str] = frozenset(
    {"person", "organization", "project", "place", "product", "event", "concept", "other"}
)


class KgEntity(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "kg_entity"
    __table_args__ = (Index("ix_kg_entity_org_norm", "org_id", "normalized_name"),)

    # 'person' | 'organization' | ... (KG_ENTITY_TYPES; DB CHECK in the migration).
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Surface/display name vs the dedupe key (casefold+collapsed whitespace).
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    # Surface forms that resolve to this canonical entity.
    aliases: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # The model that first extracted this entity (provenance; ADR-0043).
    origin_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Author identity (user OR ai_assistant, ADR-0028); nullable for backfill.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )


class KgEdge(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "kg_edge"
    __table_args__ = (
        # At most ONE OPEN current fact per typed triple. Keyed on the open
        # fact (not invalidated AND not valid-time-closed) so a superseded row
        # -- closed valid_to, still believed -- frees the slot, letting a triple
        # be re-asserted and several closed historical windows coexist
        # (migration 0068; the prior `invalidated_at IS NULL` predicate lost a
        # re-asserted "re-hire" fact). Must mirror the migration exactly.
        Index(
            "uq_kg_edge_current",
            "org_id",
            "subject_id",
            "predicate",
            "object_id",
            unique=True,
            postgresql_where=text("invalidated_at IS NULL AND valid_to IS NULL"),
        ),
        Index(
            "ix_kg_edge_subject_current",
            "org_id",
            "subject_id",
            postgresql_where=text("invalidated_at IS NULL"),
        ),
        Index(
            "ix_kg_edge_object_current",
            "org_id",
            "object_id",
            postgresql_where=text("invalidated_at IS NULL"),
        ),
        Index(
            "ix_kg_edge_review_proposed",
            "org_id",
            "created_at",
            postgresql_where=text("review_state = 'proposed'"),
        ),
    )

    subject_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("kg_entity.id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("kg_entity.id", ondelete="CASCADE"), nullable=False
    )
    # Open normalized vocabulary (lower_snake_case), e.g. works_at, located_in,
    # part_of, member_of, knows, depends_on, related_to. No DB CHECK.
    predicate: Mapped[str] = mapped_column(String(64), nullable=False)
    # --- valid-time (when the fact is true in the world) ---
    valid_from: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_to: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # --- transaction-time end (created_at is the start) ---
    # NULL = the fact is currently believed; set on contradiction/supersession.
    invalidated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    # The fact that replaced this one (chains the supersession history).
    superseded_by_edge_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("kg_edge.id", ondelete="SET NULL"), nullable=True
    )
    # 'proposed' (autonomous, awaiting adjudication) | NULL/'approved' = effective.
    review_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    origin_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="SET NULL"), nullable=True
    )
    # The note the fact was extracted from (provenance + GDPR erase handle).
    source_note_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("notes.id", ondelete="SET NULL"), nullable=True
    )


class KgEntitySource(UUIDPKMixin, OrgScopedMixin, Base):
    """N:M provenance: which note(s) extracted/referenced an entity (migration
    0069). Entities are deduped per (org, type, normalized_name) ACROSS notes,
    so one entity may be sourced by many notes -- a single ``source_note_id``
    column would be wrong. Mirrors ``blob_sources`` to make GDPR erase reach
    edge-less entities too: ``kg.erase_by_source`` drops a note's links and
    deletes any entity left with zero provenance AND zero facts (an entity
    extracted into ``entities[]`` but never used in a relation has no kg_edge,
    so the edge-orphan prune alone would never reach it)."""

    __tablename__ = "kg_entity_source"
    __table_args__ = (
        Index("uq_kg_entity_source", "org_id", "entity_id", "source_note_id", unique=True),
        Index("ix_kg_entity_source_note", "org_id", "source_note_id"),
        Index("ix_kg_entity_source_entity", "org_id", "entity_id"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("kg_entity.id", ondelete="CASCADE"), nullable=False
    )
    source_note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
