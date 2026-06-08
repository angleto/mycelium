"""FastAPI dependencies: user extraction from JWT and tenant session.

The dependency opens a ``tenant_session`` (which sets the RLS GUCs) and
verifies the user's membership in the org: isolation is enforced by the
DB, here we only authenticate/authorize.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.db import admin_session, tenant_session
from flow_core.errors import AuthError, ForbiddenError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.user import User
from flow_core.security import decode_token_async
from flow_core.services import capability_tokens
from flow_core.services.auth import assert_token_not_revoked
from flow_core.services.rbac import _RANK, get_role

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


async def current_claims(
    token: Annotated[str, Depends(_bearer_token)],
) -> dict[str, Any]:
    """Decoded bearer claims. Accepts both session JWTs (SPA / login
    flow) and agent tokens (``flow_at_...``) used by the CLI / MCP.

    For JWTs the dict carries ``sub``, ``jti``, ``iat``, ``exp``.
    For agent tokens ``sub``, ``org_id``, ``scope``, ``typ='agent'``.
    Either flavour is enough for ``current_user_id`` (reads ``sub``)
    plus the JWT revocation check (skipped gracefully when no ``jti``
    is present, since agent tokens carry their own revocation state
    enforced inside ``decode_token_async``)."""
    return await decode_token_async(token)


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


async def _active_user(user_id: uuid.UUID) -> User:
    """Look up the active User by id (global table, admin session), same
    rejection contract as ``current_user``. Used by the part-body-stream
    dep for the capability-token branch, which has the user id from the
    token rather than from a decoded JWT subject."""
    async with admin_session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError(MessageCode.AUTH_TOKEN_INVALID)
    return user


async def _resolve_user(token: str) -> User:
    """Decode a JWT / agent-token bearer, enforce jti revocation, and
    return the active User. Mirrors ``current_user_id`` + ``current_user``
    for the part-body-stream dep, which cannot reuse those FastAPI deps
    because it must first branch on a capability-token bearer."""
    claims = await decode_token_async(token)
    sub = claims.get("sub")
    if not isinstance(sub, str):
        raise AuthError(MessageCode.AUTH_TOKEN_NO_SUB)
    jti = claims.get("jti")
    if isinstance(jti, str):
        try:
            jti_uuid: uuid.UUID | None = uuid.UUID(jti)
        except ValueError:
            jti_uuid = None
        if jti_uuid is not None:
            async with admin_session() as session:
                await assert_token_not_revoked(session, jti=jti_uuid)
    return await _active_user(uuid.UUID(sub))


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


def effective_role(
    *,
    membership: Role | None,
    user: User,
    x_workspace_role: str | None,
    x_admin_mode: str | None,
) -> Role:
    """The role the request actually runs with.

    Two-step, same trust model as ``admin_mode_active`` (a header is
    honoured only up to the entitlement; a forged header can never
    escalate):

    - *ceiling* = the most this caller is entitled to. A global admin
      in admin-mode acts as ``owner`` of ANY workspace (even with no
      membership: that is the sudo escape hatch). Otherwise it is the
      caller's actual membership role; a non-admin without membership
      has no ceiling and is rejected by the caller.
    - *requested* = ``X-Workspace-Role`` parsed to a valid Role;
      absent/invalid defaults to ``member`` (least privilege: "by
      default I act as a plain user").

    The effective role is the requested role clamped DOWN to the
    ceiling: a member asking for ``owner`` stays a member."""
    if admin_mode_active(user, x_admin_mode):
        ceiling = Role.owner
    elif membership is None:
        raise ForbiddenError(MessageCode.RBAC_NO_MEMBERSHIP)
    else:
        ceiling = membership
    try:
        requested = Role(x_workspace_role) if x_workspace_role else Role.member
    except ValueError:
        requested = Role.member
    return requested if _RANK[requested] <= _RANK[ceiling] else ceiling


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
    # Set only when the request authenticated with a capability token
    # (the part-body-stream dep); the endpoint consumes it on success.
    capability_token_id: uuid.UUID | None = None


@asynccontextmanager
async def _tenant_scope(
    user: User,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None,
    *,
    x_workspace_role: str | None,
    x_admin_mode: str | None,
) -> AsyncIterator[TenantCtx]:
    """RLS-scoped tenant session shared by ``tenant_ctx`` and the
    part-body-stream dep: open the session as the human/api caller,
    resolve the sudo-clamped effective role, publish it for the
    service-layer RBAC choke point, and yield the ctx. Behaviour is
    identical to the original inline ``tenant_ctx`` body."""
    async with tenant_session(
        str(org_id),
        str(user.id),
        str(project_id) if project_id else None,
        actor_kind="human_api",
    ) as session:
        try:
            membership: Role | None = await get_role(session, org_id, user.id)
        except NotFoundError:
            membership = None
        # X-Workspace-Role is a *downgrade* lever (the SPA's per-tab
        # "act as" switch): the effective role is the requested role
        # clamped to the caller's entitlement. A global admin in
        # admin-mode is entitled to owner on any workspace; a non-admin
        # with no membership is rejected here (behaviour preserved).
        role = effective_role(
            membership=membership,
            user=user,
            x_workspace_role=x_workspace_role,
            x_admin_mode=x_admin_mode,
        )
        # Publish the sudo-clamped role for the service-layer RBAC
        # choke point (rbac.require_role), same transaction-local GUC
        # mechanism as the RLS tenant context. Without this the
        # service re-derives from stored membership and the "act as"
        # downgrade would not bind (privilege-confinement bug).
        await session.execute(
            text("SELECT set_config('app.current_role', :r, true)"),
            {"r": role.value},
        )
        yield TenantCtx(
            session=session,
            user_id=user.id,
            org_id=org_id,
            project_id=project_id,
            role=role,
        )


async def tenant_ctx(
    user: Annotated[User, Depends(current_user)],
    x_workspace_id: Annotated[str, Header()],
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    # Header is X-Workspace-Id (user-facing). Internally the tenant is
    # still org_id (RLS unchanged, ADR-0015); the rename lives here in
    # the adapter, not in core.
    org_id = uuid.UUID(x_workspace_id)
    project_id = uuid.UUID(x_project_id) if x_project_id else None
    async with _tenant_scope(
        user,
        org_id,
        project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as ctx:
        yield ctx


async def part_body_write_ctx(
    part_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Tenant context for the note-part body stream, accepting EITHER a
    normal bearer (JWT / agent token, exactly like ``tenant_ctx``) OR a
    capability token (``flow_cap_``) scoped to ``note_part_body:write``
    on this very ``part_id``.

    The capability path is confined to this endpoint:
    ``decode_token_async`` does not know ``flow_cap_``, so such a token
    is rejected everywhere else. On the capability branch the request
    runs as the token's user with a fixed ``member`` role, and
    ``capability_token_id`` is set so the endpoint consumes the token
    after the write commits."""
    if capability_tokens.is_capability_token(token):
        princ = await capability_tokens.authenticate(token)
        if princ is None:
            raise AuthError(MessageCode.CAPABILITY_TOKEN_INVALID)
        if (
            princ.action != capability_tokens.ACTION_NOTE_PART_BODY_WRITE
            or princ.resource_kind != capability_tokens.RESOURCE_NOTE_PART
            or princ.resource_id != part_id
        ):
            raise ForbiddenError(MessageCode.CAPABILITY_TOKEN_SCOPE)
        user = await _active_user(princ.user_id)
        async with tenant_session(
            str(princ.org_id),
            str(user.id),
            actor_kind="mcp_token",
            actor_subject_id=str(princ.token_id),
        ) as session:
            await session.execute(
                text("SELECT set_config('app.current_role', :r, true)"),
                {"r": Role.member.value},
            )
            yield TenantCtx(
                session=session,
                user_id=user.id,
                org_id=princ.org_id,
                project_id=None,
                role=Role.member,
                capability_token_id=princ.token_id,
            )
        return
    # Normal bearer: same contract as tenant_ctx (X-Workspace-Id required).
    if not x_workspace_id:
        raise AuthError(MessageCode.AUTH_WORKSPACE_REQUIRED)
    user = await _resolve_user(token)
    org_id = uuid.UUID(x_workspace_id)
    project_id = uuid.UUID(x_project_id) if x_project_id else None
    async with _tenant_scope(
        user,
        org_id,
        project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as ctx:
        yield ctx


async def tenant_admin_ctx(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    user: Annotated[User, Depends(current_user)],
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> TenantCtx:
    """Platform-admin surface *inside* a tenant: an RLS-scoped tenant
    session (so the write still lands in the caller's workspace, ADR
    isolation preserved) gated by the SAME sudo rule as the global
    admin surface (``require_admin``): the account must have the
    capability (``is_admin``) AND an active elevation (``X-Admin-Mode``).
    A workspace owner who is not a platform admin -- even with the
    header -- is rejected with the channel-specific code. ``current_user``
    is shared (cached) with ``tenant_ctx`` so the identity is consistent.
    """
    if not admin_mode_active(user, x_admin_mode):
        raise ForbiddenError(MessageCode.CHANNEL_ADMIN_ONLY)
    return ctx
