"""FastAPI dependencies: user extraction from JWT and tenant session.

The dependency opens a ``tenant_session`` (which sets the RLS GUCs) and
verifies the user's membership in the org: isolation is enforced by the
DB, here we only authenticate/authorize.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.db import admin_session, tenant_session
from flow_core.errors import AuthError, ForbiddenError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.user import User
from flow_core.security import decode_token
from flow_core.services.auth import assert_token_not_revoked
from flow_core.services.rbac import get_role

# auto_error=False: the scheme is published to OpenAPI (so Swagger shows
# "Authorize"), but absence/malformed credentials are reported via our
# i18n AuthError (401, code auth.missing_bearer), not FastAPI's default
# 403 {"detail": "Not authenticated"}. Keeps the error contract stable.
_bearer_scheme = HTTPBearer(auto_error=False, bearerFormat="JWT")


def _bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError(MessageCode.AUTH_MISSING_BEARER)
    return credentials.credentials


def current_claims(
    token: Annotated[str, Depends(_bearer_token)],
) -> dict[str, Any]:
    """Decoded JWT claims (sub, jti, exp). Used by logout/revoke."""
    return decode_token(token)


async def current_user_id(
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> uuid.UUID:
    sub = claims.get("sub")
    if not isinstance(sub, str):
        raise AuthError(MessageCode.AUTH_TOKEN_NO_SUB)
    # Stateless JWT + revocation list: every authenticated request
    # checks the jti against revoked_tokens (ADR-0024). Malformed jti
    # is treated as "no jti" (legacy tokens), not a 500.
    jti = claims.get("jti")
    if isinstance(jti, str):
        try:
            jti_uuid = uuid.UUID(jti)
        except ValueError:
            jti_uuid = None
        if jti_uuid is not None:
            async with admin_session() as session:
                await assert_token_not_revoked(session, jti=jti_uuid)
    return uuid.UUID(sub)


async def current_user(
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> User:
    """The authenticated User row. ``users`` is global (not RLS-scoped:
    login resolves the email before any org context), so this uses an
    admin session. A deactivated account is rejected here, so a soft
    lock (is_active=False) takes effect on the next request."""
    async with admin_session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError(MessageCode.AUTH_TOKEN_INVALID)
    return user


_TRUTHY = {"1", "true", "yes", "on"}


def admin_mode_active(
    user: User,
    x_admin_mode: str | None,
) -> bool:
    """Sudo-style elevation: an admin runs as a normal user by default
    and only acts as admin when the SPA explicitly sends X-Admin-Mode.
    The header is honoured *only* if the account actually has the
    capability, so a forged header can never escalate a non-admin
    (same trust model as costa_associati's X-Active-Role)."""
    return user.is_admin and (x_admin_mode is not None and x_admin_mode.strip().lower() in _TRUTHY)


async def require_admin(
    user: Annotated[User, Depends(current_user)],
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> User:
    """Gate for the admin surface (global user administration). Needs
    both the capability (is_admin) and an *active* elevation; without
    the elevation an admin is treated exactly like a normal user."""
    if not admin_mode_active(user, x_admin_mode):
        raise ForbiddenError(MessageCode.ADMIN_REQUIRED)
    return user


@dataclass(frozen=True, slots=True)
class TenantCtx:
    session: AsyncSession
    user_id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID | None
    role: Role


async def tenant_ctx(
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
    x_workspace_id: Annotated[str, Header()],
    x_project_id: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    # Header is X-Workspace-Id (user-facing). Internally the tenant is
    # still org_id (RLS unchanged, ADR-0015); the rename lives here in
    # the adapter, not in core.
    org_id = uuid.UUID(x_workspace_id)
    project_id = uuid.UUID(x_project_id) if x_project_id else None
    async with tenant_session(
        str(org_id), str(user_id), str(project_id) if project_id else None
    ) as session:
        try:
            role = await get_role(session, org_id, user_id)
        except NotFoundError as exc:
            raise ForbiddenError(MessageCode.RBAC_NO_MEMBERSHIP) from exc
        yield TenantCtx(
            session=session,
            user_id=user_id,
            org_id=org_id,
            project_id=project_id,
            role=role,
        )
