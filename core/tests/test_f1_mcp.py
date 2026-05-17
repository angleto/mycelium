"""F1 MCP co-equality (DB-backed): MCP tools reuse the same service
layer as REST (docs/adr/0001)."""

from __future__ import annotations

import uuid

from flow_core.db import admin_session
from flow_core.services.auth import signup
from flow_mcp.server import create_tag, create_task, list_tasks


async def test_mcp_tools_reuse_service_layer() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP",
        )
    token, org = r.token, str(r.org_id)
    tag = await create_tag(token=token, org_id=org, kind="generic", name="mcp-tag")
    await create_task(token=token, org_id=org, title="via-mcp", tag_ids=[tag["id"]])
    titles = [t["title"] for t in await list_tasks(token=token, org_id=org)]
    assert "via-mcp" in titles
