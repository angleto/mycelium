"""``whoami`` bootstrap tool (portable memory: an LLM / MCP client resumes from
Mycelium at session start instead of a machine-local file). Read-only; resolves
identity + granted scope, open assigned tasks, and a recall of the agent memory
lane (channel 'agent')."""

from __future__ import annotations

import secrets
import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.agent_token import AgentToken
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.services import identities, tasks
from mycelium_core.services.auth import signup
from mycelium_mcp.server import _PRINCIPAL, whoami


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_whoami_assistant_token_resolves_identity_scope_tasks() -> None:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="WHO")
    org, user = r.org_id, r.user_id
    handle = f"claude-{uuid.uuid4().hex[:8]}"
    async with tenant_session(str(org), str(user)) as s:
        assistant = AiAssistant(
            org_id=org,
            user_id=user,
            label="Claude",
            handle=handle,
            scope=["tasks:read", "notes:read"],
            is_active=True,
        )
        s.add(assistant)
        await s.flush()
        await identities.ensure_for_ai_assistant(s, org_id=org, assistant_id=assistant.id)
        tok = AgentToken(
            org_id=org,
            user_id=user,
            name="t",
            prefix=f"mycelium_at_{secrets.token_hex(4)}",
            token_hash=secrets.token_bytes(32),
            scope="mcp",
            assistant_id=assistant.id,
        )
        s.add(tok)
        await s.flush()
        token_id = tok.id
        task = await tasks.create_task(
            s, org_id=org, actor_id=user, title="do the thing", assignee_handle=handle
        )
        task_id = str(task.id)

    # Simulate the HTTP transport: the bearer middleware publishes the principal.
    reset = _PRINCIPAL.set((user, org, token_id))
    try:
        me = await whoami("", "")
    finally:
        _PRINCIPAL.reset(reset)

    assert me["org_id"] == str(org)
    assert me["identity"]["handle"] == handle
    assert me["identity"]["kind"] == "ai_assistant"
    assert me["identity"]["assistant_label"] == "Claude"
    # Granted scope is surfaced verbatim (this closes the "captured-but-unreadable"
    # gap for the whoami surface): null would mean full access.
    assert me["scope"] == ["tasks:read", "notes:read"]
    # The open task assigned to the assistant is surfaced.
    assert task_id in [t["id"] for t in me["open_tasks"]]
    # The durable memory lane is advertised (recall may be empty if the channel
    # is unseeded / no embedder -- a bootstrap must never fail on that).
    assert me["memory_lane"]["channel"] == "agent"
    assert isinstance(me["memory_lane"]["recall"], list)
    assert "protocol" in me["pointers"]


async def test_whoami_plain_principal_is_safe() -> None:
    # stdio / human-bearer path (token_id None): still returns a valid shape,
    # scope is None (full access), and never raises.
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="WHO2")
    org, user = r.org_id, r.user_id
    reset = _PRINCIPAL.set((user, org, None))
    try:
        me = await whoami("", "")
    finally:
        _PRINCIPAL.reset(reset)
    assert me["org_id"] == str(org)
    assert me["scope"] is None
    assert me["memory_lane"]["channel"] == "agent"
    assert isinstance(me["open_tasks"], list)
