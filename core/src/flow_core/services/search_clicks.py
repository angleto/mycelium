"""Search-click capture (ADR-0035, task 89508ca9).

The write half of the ``recall_at_k`` sensor: one append-only
``search_clicks`` row per "user ran a search and opened a result".
The read half (the recall computation over the trailing window) lives
in :mod:`flow_core.services.garden_health` next to the other sensors.

No audit-log row per click: this *is* the telemetry stream, the row is
immutable, and an audit entry would just mirror it 1:1 at double the
write volume.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.search_click import (
    SEARCH_CLICK_KINDS,
    SEARCH_CLICK_QUERY_MAX,
    SearchClick,
)
from flow_core.services.rbac import require_role


async def log_click(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    query: str,
    hit_kind: str,
    hit_id: uuid.UUID,
    rank: int,
    result_count: int,
    is_probe: bool = False,
) -> SearchClick:
    """Record one search-result click. ``rank`` is the clicked hit's
    1-based position in the ranked list the user saw; ``result_count``
    is how many ranked hits were shown. ``is_probe`` marks synthetic
    golden-fixture queries so the sensor can exclude them."""
    await require_role(session, org_id, actor_id, Role.member)
    q = query.strip()
    if not q or hit_kind not in SEARCH_CLICK_KINDS or rank < 1 or result_count < rank:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    row = SearchClick(
        org_id=org_id,
        user_id=actor_id,
        query=q[:SEARCH_CLICK_QUERY_MAX],
        hit_kind=hit_kind,
        hit_id=hit_id,
        rank=rank,
        result_count=result_count,
        is_probe=is_probe,
    )
    session.add(row)
    await session.flush()
    return row


__all__ = ["log_click"]
