"""RBAC in the service layer (single choke point for GUI/REST/MCP).

Queries run under RLS (tenant_session): membership is already filtered
to the current org; the explicit filter is defensive redundancy.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import ForbiddenError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Membership, Role

_RANK: dict[Role, int] = {
    Role.guest: 0,
    Role.member: 1,
    Role.admin: 2,
    Role.owner: 3,
}


async def get_role(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> Role:
    result = await session.execute(
        select(Membership.role).where(
            Membership.org_id == org_id,
            Membership.user_id == user_id,
        )
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise NotFoundError(MessageCode.RBAC_NO_MEMBERSHIP)
    return role


def ensure_role(current: Role, minimum: Role) -> None:
    """Pure RBAC check (no DB): raise ForbiddenError if the current
    role is below the minimum."""
    if _RANK[current] < _RANK[minimum]:
        raise ForbiddenError(
            MessageCode.RBAC_ROLE_INSUFFICIENT,
            current=current.value,
            minimum=minimum.value,
        )


async def require_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    minimum: Role,
) -> Role:
    role = await get_role(session, org_id, user_id)
    ensure_role(role, minimum)
    return role
