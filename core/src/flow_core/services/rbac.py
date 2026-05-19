"""RBAC in the service layer (single choke point for GUI/REST/MCP).

Queries run under RLS (tenant_session): membership is already filtered
to the current org; the explicit filter is defensive redundancy.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
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


async def effective_request_role(session: AsyncSession) -> Role | None:
    """The sudo-clamped role the current request runs *as*, published
    by the API boundary (``tenant_ctx``) as the transaction-local GUC
    ``app.current_role`` (same mechanism as ``app.current_org`` for
    RLS). It is already clamped DOWN to the caller's entitlement, so
    honouring it can only de-escalate, never escalate.

    ``None`` when unset: background workers, ``admin_session`` and
    direct service/unit tests do not pass through ``tenant_ctx`` and
    keep the stored-membership behaviour (fallback below)."""
    raw = (await session.execute(text("SELECT current_setting('app.current_role', true)"))).scalar()
    if not raw:
        return None
    try:
        return Role(raw)
    except ValueError:
        return None


async def require_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    minimum: Role,
) -> Role:
    """RBAC choke point. Enforces the *effective* role the request
    acts as (the SPA's "act as" lever / sudo de-escalation): a
    workspace owner who dropped to ``user`` is denied owner/admin
    operations even though their stored membership is higher. Falls
    back to stored membership when no request role is published
    (workers/tests)."""
    role = await effective_request_role(session)
    if role is None:
        role = await get_role(session, org_id, user_id)
    ensure_role(role, minimum)
    return role
