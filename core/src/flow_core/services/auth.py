"""Signup, login and org resolution.

These run without a tenant context (admin_session): ``users`` is not
org-scoped and org+membership creation goes through the SECURITY
DEFINER function ``provision_organization`` (docs/adr/0015), the single
place that creates a tenant. Listing the orgs a user belongs to is also
pre-tenant (no org context yet) and goes through the SECURITY DEFINER
function ``list_user_organizations`` (migration 0014).

Vocabulary note: the core/domain term is ``org`` (RLS, ADR-0015,
unchanged). The user-facing rename to "workspace" lives only in the
API/MCP adapters, not here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import AuthError
from flow_core.i18n import MessageCode
from flow_core.models.user import User
from flow_core.security import create_access_token, hash_password, verify_password


@dataclass(frozen=True, slots=True)
class SignupResult:
    user_id: uuid.UUID
    org_id: uuid.UUID
    token: str


@dataclass(frozen=True, slots=True)
class OrgMembership:
    """An org the user belongs to, with their role in it."""

    id: uuid.UUID
    name: str
    role: str


async def _provision_org(session: AsyncSession, *, name: str, user_id: uuid.UUID) -> uuid.UUID:
    result = await session.execute(
        text("SELECT provision_organization(:n, :u)"),
        {"n": name, "u": str(user_id)},
    )
    return uuid.UUID(str(result.scalar_one()))


async def signup(
    session: AsyncSession, *, email: str, password: str, org_name: str
) -> SignupResult:
    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    await session.flush()  # populate user.id
    org_id = await _provision_org(session, name=org_name, user_id=user.id)
    return SignupResult(
        user_id=user.id,
        org_id=org_id,
        token=create_access_token(user_id=str(user.id)),
    )


async def create_org_for_user(
    session: AsyncSession, *, user_id: uuid.UUID, name: str
) -> uuid.UUID:
    """Create an additional org for an existing authenticated user (they
    become its owner). Powers in-app workspace creation, no re-auth."""
    return await _provision_org(session, name=name, user_id=user_id)


async def list_user_orgs(
    session: AsyncSession, *, user_id: uuid.UUID
) -> list[OrgMembership]:
    """Orgs the user belongs to (pre-tenant; for the in-app switcher).
    Crosses the RLS boundary only via the SECURITY DEFINER
    ``list_user_organizations`` function (migration 0014)."""
    rows = await session.execute(
        text("SELECT org_id, name, role FROM list_user_organizations(:u) ORDER BY name"),
        {"u": str(user_id)},
    )
    return [
        OrgMembership(id=uuid.UUID(str(r.org_id)), name=str(r.name), role=str(r.role))
        for r in rows
    ]


async def login(session: AsyncSession, *, email: str, password: str) -> str:
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise AuthError(MessageCode.AUTH_INVALID_CREDENTIALS)
    return create_access_token(user_id=str(user.id))
