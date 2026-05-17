"""MemoryBlob: scheletro F0 della memoria gerarchica.

Solo struttura sufficiente a dimostrare l'isolamento duro per
(org, progetto): RLS + partizione per org + predicato obbligatorio
(docs/adr/0005, 0007). Embedding/tiering/RRF arrivano in F6.

La tabella e ``PARTITION BY HASH (org_id)`` nella migrazione baseline:
il key di partizione e org_id (bound dimensione indici per tenant); la
RLS resta il confine di sicurezza. Le partizioni LIST per-org sono un
raffinamento futuro quando il numero di tenant lo giustifica.
"""

from __future__ import annotations

import uuid

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, OrgScopedMixin, TimestampMixin, UUIDPKMixin


class MemoryBlob(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "memory_blobs"

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    namespace: Mapped[str] = mapped_column(String(40), nullable=False, server_default="email")
    tier: Mapped[str] = mapped_column(String(8), nullable=False, server_default="hot")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
