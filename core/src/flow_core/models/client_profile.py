"""Typed satellite profile for tags of kind ``client`` (docs/adr/0003).
1:1 with a tag; PK is the tag id."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    VersionMixin,
)


class ClientProfile(OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "client_profile"

    tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ragione_sociale: Mapped[str] = mapped_column(String(200), nullable=False)
    id_paese: Mapped[str | None] = mapped_column(String(2), nullable=True)
    id_codice: Mapped[str | None] = mapped_column(String(30), nullable=True)
    codice_fiscale: Mapped[str | None] = mapped_column(String(30), nullable=True)
    indirizzo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cap: Mapped[str | None] = mapped_column(String(10), nullable=True)
    comune: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provincia: Mapped[str | None] = mapped_column(String(4), nullable=True)
    nazione: Mapped[str | None] = mapped_column(String(2), nullable=True)
    codice_destinatario: Mapped[str | None] = mapped_column(String(7), nullable=True)
    pec: Mapped[str | None] = mapped_column(String(320), nullable=True)
