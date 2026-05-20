"""Agent tokens (v1.1) service + decode_token_async tests.

Real DB, no mocking of domain logic. Covers:

- mint returns ``raw`` exactly once and its sha256 matches the stored hash
- list_tokens is RLS-scoped (a foreign workspace sees zero rows)
- authenticate succeeds with the raw, fails for tampered / revoked / expired
- last_used_at bumps on a successful authenticate
- decode_token_async accepts a JWT (existing path) and an agent token
- agent-token-bound claims carry ``org_id`` + ``scope='mcp'`` + ``typ='agent'``
"""

from __future__ import annotations

import datetime
import hashlib
import uuid

import pytest
from sqlalchemy import select, update

from flow_core.db import admin_session, tenant_session
from flow_core.errors import AuthError, ForbiddenError
from flow_core.i18n import MessageCode
from flow_core.models.agent_token import AgentToken
from flow_core.models.membership import Role
from flow_core.security import decode_token_async
from flow_core.services import agent_tokens as svc
from flow_core.services.auth import login, signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup() -> tuple[uuid.UUID, uuid.UUID, str]:
    """Returns (org_id, user_id, jwt_token)."""
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="AT")
    return r.org_id, r.user_id, r.token


async def _login_jwt(email: str, password: str) -> str:
    async with admin_session() as s:
        r = await login(s, email=email, password=password)
    return r.token


# ---------------------------------------------------------------------------
# mint / list / revoke
# ---------------------------------------------------------------------------


async def test_mint_returns_raw_and_stores_only_hash() -> None:
    org, user, _ = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        result = await svc.mint(s, org_id=org, actor_id=user, name="Claude Desktop")
    assert result.raw.startswith(svc.RAW_PREFIX)
    assert result.token.prefix == result.raw[:16]
    # The hash on the row is sha256 of the raw bytes
    expected_hash = hashlib.sha256(result.raw.encode("utf-8")).digest()
    assert result.token.token_hash == expected_hash
    # name + scope round-trip
    assert result.token.name == "Claude Desktop"
    assert result.token.scope == "mcp"
    # expires_at populated by default TTL
    assert result.token.expires_at is not None


async def test_mint_owner_gated_member_forbidden() -> None:
    # First user becomes owner of org A; invite a second user as plain
    # member and verify they cannot mint inside that workspace.
    org_a, owner, _ = await _signup()
    _, second_user, _ = await _signup()
    # Add the second user as a member to org_a (not owner).
    from flow_core.models.membership import Membership

    async with tenant_session(str(org_a), str(owner)) as s:
        s.add(Membership(org_id=org_a, user_id=second_user, role=Role.member))
        await s.flush()
    async with tenant_session(str(org_a), str(second_user)) as s:
        with pytest.raises(ForbiddenError):
            await svc.mint(s, org_id=org_a, actor_id=second_user, name="nope")


async def test_list_tokens_rls_isolated() -> None:
    org_a, user_a, _ = await _signup()
    org_b, user_b, _ = await _signup()
    async with tenant_session(str(org_a), str(user_a)) as s:
        await svc.mint(s, org_id=org_a, actor_id=user_a, name="A1")
        await svc.mint(s, org_id=org_a, actor_id=user_a, name="A2")
    async with tenant_session(str(org_b), str(user_b)) as s:
        rows = await svc.list_tokens(s, org_id=org_b)
    # Org B sees only its own (empty) set, not org A's two tokens.
    assert rows == []


async def test_list_tokens_returns_workspace_set() -> None:
    org, user, _ = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        await svc.mint(s, org_id=org, actor_id=user, name="first")
        await svc.mint(s, org_id=org, actor_id=user, name="second")
        rows = await svc.list_tokens(s, org_id=org)
    # Same-transaction inserts share ``now()`` for created_at, so the
    # order within them is not meaningful; assert membership.
    assert {r.name for r in rows} == {"first", "second"}


async def test_revoke_marks_revoked_at_and_is_idempotent() -> None:
    org, user, _ = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="x")
        token_id = r.token.id
    async with tenant_session(str(org), str(user)) as s:
        await svc.revoke(s, org_id=org, actor_id=user, token_id=token_id)
        row = (await s.execute(select(AgentToken).where(AgentToken.id == token_id))).scalar_one()
        first_revoked = row.revoked_at
        assert first_revoked is not None
        # Idempotent re-revoke preserves the timestamp.
        await svc.revoke(s, org_id=org, actor_id=user, token_id=token_id)
        row2 = (await s.execute(select(AgentToken).where(AgentToken.id == token_id))).scalar_one()
        assert row2.revoked_at == first_revoked


# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------


async def test_authenticate_resolves_principal_and_bumps_last_used_at() -> None:
    org, user, _ = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="cli")
    raw = r.raw

    # First authenticate -- last_used_at goes from NULL to a timestamp
    result = await svc.authenticate(raw)
    assert result is not None
    assert result.user_id == user
    assert result.org_id == org
    assert result.scope == "mcp"

    async with tenant_session(str(org), str(user)) as s:
        row = (await s.execute(select(AgentToken).where(AgentToken.id == r.token.id))).scalar_one()
        first_bump = row.last_used_at
        assert first_bump is not None


async def test_authenticate_rejects_tampered_token() -> None:
    org, user, _ = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="cli")
    # Flip the last character; the hash will not match.
    tampered = r.raw[:-1] + ("Z" if r.raw[-1] != "Z" else "Y")
    assert await svc.authenticate(tampered) is None


async def test_authenticate_rejects_revoked_token() -> None:
    org, user, _ = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="cli")
        await svc.revoke(s, org_id=org, actor_id=user, token_id=r.token.id)
    assert await svc.authenticate(r.raw) is None


async def test_authenticate_rejects_expired_token() -> None:
    org, user, _ = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="cli")
        # Force-expire by rewinding expires_at to the past.
        await s.execute(
            update(AgentToken)
            .where(AgentToken.id == r.token.id)
            .values(expires_at=datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=1))
        )
    assert await svc.authenticate(r.raw) is None


async def test_authenticate_returns_none_for_unknown_token() -> None:
    fake = f"{svc.RAW_PREFIX}{'A' * 43}"
    assert await svc.authenticate(fake) is None


async def test_authenticate_returns_none_for_non_agent_token() -> None:
    # Anything that does not carry the discriminator prefix.
    assert await svc.authenticate("not.an.agent.token") is None


# ---------------------------------------------------------------------------
# decode_token_async (the wrapper the MCP / future bearer paths call)
# ---------------------------------------------------------------------------


async def test_decode_token_async_jwt_path_unchanged() -> None:
    _, user, jwt_token = await _signup()
    claims = await decode_token_async(jwt_token)
    assert claims["sub"] == str(user)
    # JWT-only claim that an agent token never carries
    assert "exp" in claims
    assert claims.get("typ") != "agent"


async def test_decode_token_async_agent_token_path() -> None:
    org, user, _ = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="cli")
    claims = await decode_token_async(r.raw)
    assert claims["sub"] == str(user)
    assert claims["org_id"] == str(org)
    assert claims["scope"] == "mcp"
    assert claims["typ"] == "agent"


async def test_decode_token_async_revoked_raises_auth_error() -> None:
    org, user, _ = await _signup()
    async with tenant_session(str(org), str(user)) as s:
        r = await svc.mint(s, org_id=org, actor_id=user, name="cli")
        await svc.revoke(s, org_id=org, actor_id=user, token_id=r.token.id)
    with pytest.raises(AuthError) as exc_info:
        await decode_token_async(r.raw)
    assert exc_info.value.code == MessageCode.AGENT_TOKEN_INVALID
