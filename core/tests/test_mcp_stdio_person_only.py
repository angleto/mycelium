"""The stdio transport is a person's: an agent token does not open it.

stdio has no scope gate on a direct tool call and opens its session as
``human_api``, so whoever opens it acts as a person. The token decoder it
used accepts an agent token too, and a bare agent token on stdio reached
``accept_suggestion`` and changed a task's text, recorded as a person's
decision (verification of task f34a0292, 2026-11-16). Agent tokens connect
over HTTP at /mcp, where they are confined.
"""

from __future__ import annotations

import uuid

import pytest

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import AuthError
from mycelium_core.i18n import MessageCode
from mycelium_core.services import agent_tokens as agent_tokens_svc
from mycelium_core.services.auth import signup
from mycelium_mcp.server import _PRINCIPAL, whoami


async def _person_and_agent_token() -> tuple[str, str, str]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="STDIO",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        minted = await agent_tokens_svc.mint(
            s, org_id=r.org_id, actor_id=r.user_id, name="cli", scope="cli"
        )
    return r.token, minted.raw, str(r.org_id)


async def test_an_agent_token_does_not_open_the_stdio_transport() -> None:
    assert _PRINCIPAL.get() is None  # the stdio path: no HTTP principal
    _jwt, raw, org = await _person_and_agent_token()
    with pytest.raises(AuthError) as caught:
        await whoami(token=raw, org_id=org)
    assert caught.value.code == MessageCode.AUTH_STDIO_PERSON_ONLY


async def test_a_person_session_token_still_opens_it() -> None:
    jwt, _raw, org = await _person_and_agent_token()
    me = await whoami(token=jwt, org_id=org)
    assert me
