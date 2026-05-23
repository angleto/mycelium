"""Append-only activity log helper (docs/adr/0002).

Insert-only; never update/delete (DB trigger enforces it). Reads
``actor_kind`` and ``actor_subject_id`` from the session GUCs set by
``db.tenant_session`` / ``db.admin_session`` / ``db.with_actor`` and
persists them on each row. The 121 call sites do not need to know
the caller type: it travels with the session.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.activity_log import ActivityLog


async def log(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    entity: str,
    entity_id: uuid.UUID | None,
    action: str,
    diff: Mapping[str, Any] | None = None,
) -> None:
    row = (
        await session.execute(
            text(
                "SELECT current_setting('app.current_actor_kind', true),"
                "       current_setting('app.current_actor_subject', true)"
            )
        )
    ).one()
    actor_kind = row[0] or "human_direct"
    actor_subject_raw = row[1] or ""
    actor_subject_id: uuid.UUID | None
    try:
        actor_subject_id = uuid.UUID(actor_subject_raw) if actor_subject_raw else None
    except ValueError:
        actor_subject_id = None
    session.add(
        ActivityLog(
            org_id=org_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            actor_subject_id=actor_subject_id,
            entity=entity,
            entity_id=entity_id,
            action=action,
            diff=dict(diff) if diff is not None else None,
        )
    )
    await session.flush()
