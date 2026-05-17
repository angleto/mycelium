"""RBAC nel service layer (unico choke point per GUI/REST/MCP).

Le query girano sotto RLS (tenant_session): la membership e gia
filtrata sull'org corrente; il filtro esplicito e ridondanza difensiva.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import ForbiddenError, NotFoundError
from flow_core.models.membership import Membership, Role

_RANK: dict[Role, int] = {
    Role.guest: 0,
    Role.member: 1,
    Role.admin: 2,
    Role.owner: 3,
}


async def get_role(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> Role:
    result = await session.execute(
        select(Membership.role).where(
            Membership.org_id == org_id,
            Membership.user_id == user_id,
        )
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise NotFoundError("nessuna membership nell'organizzazione")
    return role


async def require_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    minimum: Role,
) -> Role:
    role = await get_role(session, org_id, user_id)
    if _RANK[role] < _RANK[minimum]:
        raise ForbiddenError(
            f"ruolo {role.value} insufficiente, richiesto >= {minimum.value}"
        )
    return role
