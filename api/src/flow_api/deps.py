"""FastAPI dependencies: user extraction from JWT and tenant session.

The dependency opens a ``tenant_session`` (which sets the RLS GUCs) and
verifies the user's membership in the org: isolation is enforced by the
DB, here we only authenticate/authorize.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.db import tenant_session
from flow_core.errors import AuthError, ForbiddenError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.security import decode_token
from flow_core.services.rbac import get_role

# auto_error=False: the scheme is published to OpenAPI (so Swagger shows
# "Authorize"), but absence/malformed credentials are reported via our
# i18n AuthError (401, code auth.missing_bearer), not FastAPI's default
# 403 {"detail": "Not authenticated"}. Keeps the error contract stable.
_bearer_scheme = HTTPBearer(auto_error=False, bearerFormat="JWT")


def _bearer_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ] = None,
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError(MessageCode.AUTH_MISSING_BEARER)
    return credentials.credentials


def current_user_id(
    token: Annotated[str, Depends(_bearer_token)],
) -> uuid.UUID:
    claims = decode_token(token)
    sub = claims.get("sub")
    if not isinstance(sub, str):
        raise AuthError(MessageCode.AUTH_TOKEN_NO_SUB)
    return uuid.UUID(sub)


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
