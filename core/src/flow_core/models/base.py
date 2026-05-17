"""Declarative base e mixin comuni.

- naming convention stabile (migrazioni Alembic deterministiche)
- uuid pk, timestamp, org scope, ``version`` per optimistic concurrency

L'optimistic concurrency e applicata in modo esplicito dal service
layer (UPDATE ... WHERE id AND version; 0 righe -> ConflictError),
come da docs/adr/0002: niente magia ORM implicita.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPKMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrgScopedMixin:
    """Ogni riga appartiene a una org. La RLS filtra su questa colonna."""

    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)


class VersionMixin:
    """Contatore di versione per optimistic concurrency (incrementato
    esplicitamente dal service layer ad ogni update)."""

    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
