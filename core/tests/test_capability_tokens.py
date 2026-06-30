"""Capability-token service: mint / authenticate / consume / expiry.

Mirrors ``test_agent_tokens.py`` (signup -> tenant_session -> service
call). Exercises the single-use + short-TTL guarantees and the
SECURITY DEFINER ``authenticate_capability_token`` lookup (migration
0038), which must find the row across the tenant boundary.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import capability_tokens as svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="CT")
    return r.org_id, r.user_id


async def _mint(
    org: uuid.UUID, user: uuid.UUID, *, resource_id: uuid.UUID | None = None, ttl_seconds: int = 300
) -> tuple[svc.MintResult, uuid.UUID]:
    rid = resource_id or uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.mint(
            s,
            org_id=org,
            actor_id=user,
            action=svc.ACTION_NOTE_PART_BODY_WRITE,
            resource_kind=svc.RESOURCE_NOTE_PART,
            resource_id=rid,
            ttl_seconds=ttl_seconds,
        )
    return res, rid


async def test_mint_stores_only_hash_and_prefix() -> None:
    org, user = await _signup()
    res, _ = await _mint(org, user)
    assert res.raw.startswith(svc.RAW_PREFIX)
    # Read back inside the tenant: capability_tokens has RLS, and flow_app
    # (the app role) only sees rows for the current org. An admin_session
    # with no org GUC is correctly blind to it -- in production the only
    # cross-tenant read is the SECURITY DEFINER verify function.
    async with tenant_session(str(org), str(user)) as s:
        row = (
            await s.execute(
                text("SELECT token_hash, prefix, consumed_at FROM capability_tokens WHERE id = :i"),
                {"i": res.token_id},
            )
        ).first()
    assert row is not None
    assert bytes(row[0]) == hashlib.sha256(res.raw.encode("utf-8")).digest()
    assert row[1] == res.raw[:16]
    assert row[2] is None  # not consumed at mint time


async def test_authenticate_returns_principal_and_constraint() -> None:
    org, user = await _signup()
    res, rid = await _mint(org, user)
    auth = await svc.authenticate(res.raw)
    assert auth is not None
    assert auth.token_id == res.token_id
    assert auth.org_id == org
    assert auth.user_id == user
    assert auth.action == svc.ACTION_NOTE_PART_BODY_WRITE
    assert auth.resource_kind == svc.RESOURCE_NOTE_PART
    assert auth.resource_id == rid


async def test_authenticate_rejects_unknown_and_wrong_prefix() -> None:
    assert await svc.authenticate("mycelium_cap_does-not-exist") is None
    # An agent-token prefix is not a capability token.
    assert await svc.authenticate("flow_at_whatever") is None


async def test_consume_is_single_use() -> None:
    org, user = await _signup()
    res, _ = await _mint(org, user)
    async with tenant_session(str(org), str(user)) as s:
        first = await svc.consume(s, token_id=res.token_id)
    assert first is True
    # Consumed: authentication now fails and a second consume is a no-op.
    assert await svc.authenticate(res.raw) is None
    async with tenant_session(str(org), str(user)) as s:
        second = await svc.consume(s, token_id=res.token_id)
    assert second is False


async def test_authenticate_rejects_expired() -> None:
    org, user = await _signup()
    res, _ = await _mint(org, user)
    async with tenant_session(str(org), str(user)) as s:
        await s.execute(
            text(
                "UPDATE capability_tokens SET expires_at = now() - interval '1 minute' "
                "WHERE id = :i"
            ),
            {"i": res.token_id},
        )
    assert await svc.authenticate(res.raw) is None
