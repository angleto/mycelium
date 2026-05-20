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

from flow_core.db import admin_session, tenant_session
from flow_core.services import agent_tokens as svc
from flow_core.services.auth import signup
from flow_mcp.server import list_tags


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
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="claude")
    raw = r.raw
    # The positional org_id may be empty; the agent token carries it.
    tags = await list_tags(token=raw, org_id="")
    # The fresh workspace has no tags yet (no canonical seed of generic
    # tags), so the empty list confirms the call succeeded under
    # tenant_session opened against the token-carried workspace.
    assert tags == []


async def test_mcp_tenant_rejects_revoked_agent_token() -> None:
    org, user = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="claude")
        await svc.revoke(s, org_id=org, actor_id=user, token_id=r.token.id)

    from flow_core.errors import AuthError

    try:
        await list_tags(token=r.raw, org_id="")
    except AuthError:
        return
    raise AssertionError("revoked agent token should not authenticate via MCP")


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
