"""Declarative base and common mixins.

- a stable naming convention (deterministic Alembic migrations)
- uuid pk, timestamps, org scope, ``version`` for optimistic concurrency

Optimistic concurrency is enforced explicitly by the service layer
(UPDATE ... WHERE id AND version; 0 rows -> ConflictError), per
docs/adr/0002: no implicit ORM magic.
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
    """Every row belongs to an org. RLS filters on this column."""

    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)


class VersionMixin:
    """Version counter for optimistic concurrency (incremented
    explicitly by the service layer on every update)."""

    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
