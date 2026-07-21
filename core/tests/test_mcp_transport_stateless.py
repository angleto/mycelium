"""MCP streamable-http transport runs stateless (task c19f2f63, enabler B).

In stateful mode the SDK starts ONE long-lived per-session task at
``initialize`` and anyio copies the caller's context into it, so the
principal / scope published by the bearer middleware is frozen for the
session's whole life (sessions never expire) and the ``Mcp-Session-Id``
becomes unbound ambient authority a leaked id can replay. Stateless starts a
FRESH per-request task, so every request runs with the scope of the token it
actually carries, and there is no session id to replay.

Two things are pinned:
1. the session manager is actually built stateless (a regression would
   silently reintroduce the freeze);
2. end to end over the real transport, a self-contained ``tools/call`` with NO
   prior ``initialize`` is answered (only a pre-initialized stateless session
   does that) and each request is authenticated on its own bearer.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import agent_tokens as svc
from mycelium_core.services.auth import signup
from mycelium_mcp.gateway import gateway
from mycelium_mcp.server_http import make_mcp_app


def test_session_manager_is_built_stateless() -> None:
    make_mcp_app()
    assert gateway.settings.stateless_http is True
    assert gateway._session_manager is not None
    assert gateway._session_manager.stateless is True


async def _mint_token() -> str:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="STATELESS",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        m = await svc.mint(s, org_id=r.org_id, actor_id=r.user_id, name="stateless")
    return m.raw


async def test_standalone_tool_call_needs_no_session() -> None:
    """The connector-relevant property: a request is self-contained. A
    ``tools/call`` sent WITHOUT a prior ``initialize`` on the connection is
    answered (a stateful session would reject it as pre-initialization), no
    session id is handed back, and a bad bearer on the same live app is
    rejected on its own -- proving auth is per request, not per session."""
    raw = await _mint_token()
    app = make_mcp_app()
    headers = {
        "Authorization": f"Bearer {raw}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "ping", "arguments": {}},
    }
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://mcp") as c:
            resp = await c.post("/", headers=headers, json=call)
            assert resp.status_code == 200, resp.text
            # stateless: the server tracks no session, so it never issues an id.
            assert "mcp-session-id" not in {k.lower() for k in resp.headers}
            assert "mycelium-core" in resp.text  # the ping result, spliced into SSE

            bad = await c.post(
                "/",
                headers={**headers, "Authorization": "Bearer mycelium_at_bogus"},
                json=call,
            )
            assert bad.status_code == 401, bad.text
