"""MCP server accepts agent tokens (v1.1).

The MCP ``_tenant`` helper used to take a session JWT only. After v1.1
A it accepts either a JWT (existing behaviour) or a long-lived agent
token (``flow_at_...``). For the agent-token path the workspace is
intrinsic to the credential, so the positional ``org_id`` may be
empty.

Real DB + a real minted token, no mocks.
"""

from __future__ import annotations

import uuid

from tests_helpers import as_agent

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import agent_tokens as svc
from mycelium_core.services.auth import signup
from mycelium_mcp.server import list_tags


async def _signup() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP-AT",
        )
    return r.org_id, r.user_id


async def test_mcp_tenant_accepts_agent_token() -> None:
    """Over HTTP, where an agent token reaches the tools: the bearer
    middleware publishes the principal and the call opens the token's
    workspace. stdio refuses it (``test_mcp_stdio_person_only``)."""
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="claude")
    async with as_agent(r.raw):
        tags = await list_tags(token="", org_id="")
    # The fresh workspace has no tags yet (no canonical seed of generic
    # tags), so the empty list confirms the call succeeded under
    # tenant_session opened against the token-carried workspace.
    assert tags == []


async def test_mcp_tenant_rejects_revoked_agent_token() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="claude")
        await svc.revoke(s, org_id=org, actor_id=user, token_id=r.token.id)

    from mycelium_core.errors import AuthError

    # The HTTP bearer middleware authenticates through this call; a revoked
    # token does not get a principal, so it never reaches a tool.
    assert await svc.authenticate(r.raw) is None
    # Nor does stdio take it, revoked or not.
    try:
        await list_tags(token=r.raw, org_id="")
    except AuthError:
        return
    raise AssertionError("an agent token should not open the stdio transport")


async def test_mcp_tenant_still_accepts_jwt_unchanged() -> None:
    # The JWT path (existing behaviour) must keep working.
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP-JWT",
        )
    assert r.token is not None
    tags = await list_tags(token=r.token, org_id=str(r.org_id))
    assert tags == []
