"""Explicit optimistic concurrency (docs/adr/0002).

UPDATE ... WHERE id AND version = expected; 0 rows -> ConflictError
(adapters map it to HTTP 409). No implicit ORM magic. The service
layer is the only place that mutates versioned state.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from flow_core.errors import ConflictError
from flow_core.i18n import MessageCode


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
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
    return int(row[0])
