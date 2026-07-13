"""Explicit optimistic concurrency (docs/adr/0002).

UPDATE ... WHERE id AND version = expected; 0 rows -> ConflictError
(adapters map it to HTTP 409). No implicit ORM magic. The service
layer is the only place that mutates versioned state.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from mycelium_core.errors import ConflictError
from mycelium_core.i18n import MessageCode


async def optimistic_update(
    session: AsyncSession,
    model: type[DeclarativeBase],
    *,
    pk: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> int:
    """Apply the update only if the version matches; return the new
    version. Raise ConflictError if the row is stale or missing.

    Tenant isolation is guaranteed by RLS (primary defense,
    docs/adr/0002): the UPDATE can only touch rows visible in the
    current tenant context, so pk + version is sufficient here.
    """
    table = model.__table__
    stmt = (
        update(model)
        .where(
            table.c.id == pk,
            table.c.version == expected_version,
        )
        .values(**values, version=table.c.version + 1)
        .returning(table.c.version)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        # Stale (version mismatch) or missing (RLS/deleted). Surface the
        # CURRENT version so the caller can re-read and reconcile instead of
        # failing blind: a human+agent co-editing one comment hit this, and an
        # opaque "can't save" read as a permissions problem (task 515e13fb).
        # An extra param is ignored by the (field-less) message template.
        current = (
            await session.execute(select(table.c.version).where(table.c.id == pk))
        ).scalar_one_or_none()
        if current is None:
            raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION, current_version=int(current))
    return int(row[0])
