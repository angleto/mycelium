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

from mycelium_core.errors import ConflictError, UnprocessableError
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
    # An explicit ``null`` for a NOT NULL column can only ever end as an
    # IntegrityError, i.e. a 500 for what is a client mistake. Both PATCH
    # surfaces can produce one: a field typed ``T | None = None`` accepts a
    # stated null and ``exclude_unset`` / ``model_fields_set`` keep it, which
    # is how ``{"index_scope": null}`` and ``{"necessity": null}`` reach here.
    # Refused once, in the single funnel for versioned writes, rather than
    # per field on each surface. Nothing legitimate is lost: the write could
    # not have succeeded.
    nulled = sorted(
        k for k, v in values.items() if v is None and k in table.c and not table.c[k].nullable
    )
    if nulled:
        raise UnprocessableError(MessageCode.FIELD_NOT_NULLABLE, field=nulled[0])
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
