"""Append-only activity log helper (docs/adr/0002).

Insert-only; never update/delete (DB trigger enforces it).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

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
    session.add(
        ActivityLog(
            org_id=org_id,
            actor_id=actor_id,
            entity=entity,
            entity_id=entity_id,
            action=action,
            diff=dict(diff) if diff is not None else None,
        )
    )
    await session.flush()
