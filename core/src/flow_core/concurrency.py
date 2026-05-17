"""Optimistic concurrency esplicita (docs/adr/0002).

UPDATE ... WHERE id AND org_id AND version = atteso; 0 righe ->
ConflictError (l'adapter lo mappa a 409). Niente magia ORM implicita.
Il service layer e l'unico punto che muta lo stato versionato.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Table, update
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import ConflictError


async def optimistic_update(
    session: AsyncSession,
    table: Table,
    *,
    pk: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> int:
    """Applica l'update solo se la versione combacia; ritorna la nuova
    versione. Solleva ConflictError se la riga e stale o assente.

    L'isolamento tenant e garantito dalla RLS (difesa primaria,
    docs/adr/0002): l'UPDATE puo toccare solo righe visibili nel
    contesto tenant corrente, quindi qui basta pk + version.
    """
    stmt = (
        update(table)
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
        raise ConflictError("scrittura su versione stale")
    return int(row[0])
