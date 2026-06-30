"""AI assistant service: create, list, update, delete, rotate."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.mcp_scopes import DEFAULT_SCOPES, VALID_SCOPE_KEYS
from mycelium_core.models.agent_token import AgentToken
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.services import ai_assistants as svc
from mycelium_core.services.auth import signup


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP",
        )
    return r.org_id, r.user_id


async def test_create_assistant_returns_secret_once_and_persists_metadata() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.create_assistant(s, org_id=org, actor_id=user, label="Claude")
        assert res.raw_secret.startswith("mycelium_at_")
        assert res.token_prefix.startswith("mycelium_at_")
        assert res.assistant.label == "Claude"
        # Default scope = everything except 'danger'.
        assert set(res.assistant.scope_list()) == set(DEFAULT_SCOPES)
        # Exactly one agent_token bound to the new assistant.
        rows = (
            (await s.execute(select(AgentToken).where(AgentToken.assistant_id == res.assistant.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].revoked_at is None


async def test_create_assistant_rejects_unknown_scope() -> None:
    org, user = await _org()
    with pytest.raises(DomainError) as ei:
        async with tenant_session(str(org), str(user)) as s:
            await svc.create_assistant(
                s,
                org_id=org,
                actor_id=user,
                label="X",
                scope=["tasks:read", "bogus:scope"],
            )
    assert ei.value.code is MessageCode.AI_ASSISTANT_INVALID_SCOPE


async def test_rotate_secret_revokes_old_token_and_mints_new() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        first = await svc.create_assistant(s, org_id=org, actor_id=user, label="R")
    async with tenant_session(str(org), str(user)) as s:
        rotated = await svc.rotate_secret(
            s, org_id=org, actor_id=user, assistant_id=first.assistant.id
        )
        assert rotated.raw_secret != first.raw_secret
        rows = (
            (
                await s.execute(
                    select(AgentToken).where(AgentToken.assistant_id == first.assistant.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        active = [r for r in rows if r.revoked_at is None]
        revoked = [r for r in rows if r.revoked_at is not None]
        assert len(active) == 1 and len(revoked) == 1


async def test_delete_assistant_cascades_tokens() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.create_assistant(s, org_id=org, actor_id=user, label="D")
        await svc.delete_assistant(s, org_id=org, actor_id=user, assistant_id=res.assistant.id)
    async with tenant_session(str(org), str(user)) as s:
        gone = (
            await s.execute(select(AiAssistant).where(AiAssistant.id == res.assistant.id))
        ).scalar_one_or_none()
        assert gone is None
        # FK CASCADE: every token bound to the assistant is gone too.
        leftover = (
            (await s.execute(select(AgentToken).where(AgentToken.assistant_id == res.assistant.id)))
            .scalars()
            .all()
        )
        assert leftover == []


async def test_get_assistant_rejects_other_users_row() -> None:
    org, user_a = await _org()
    org_b, user_b = await _org()
    async with tenant_session(str(org), str(user_a)) as s:
        res = await svc.create_assistant(s, org_id=org, actor_id=user_a, label="A")
    # user_b lives in a different workspace; RLS makes the row invisible.
    with pytest.raises(NotFoundError):
        async with tenant_session(str(org_b), str(user_b)) as s:
            await svc.get_assistant(
                s,
                org_id=org_b,
                user_id=user_b,
                assistant_id=res.assistant.id,
            )


async def test_default_scopes_are_a_subset_of_catalog() -> None:
    # Trivial invariant — guards against typos in mcp_scopes.py.
    assert set(DEFAULT_SCOPES).issubset(VALID_SCOPE_KEYS)
