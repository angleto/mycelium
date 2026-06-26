"""F1 MCP co-equality (DB-backed): MCP tools reuse the same service
layer as REST (docs/adr/0001)."""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup
from mycelium_mcp.server import create_tag, create_task, get_task, list_tasks


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


async def test_get_task_and_list_carry_tags() -> None:
    """Regression: get_task / list_tasks must surface a task's tags so
    an MCP caller can see them without a separate list_tags round-trip."""
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP-tags",
        )
    token, org = r.token, str(r.org_id)
    tag = await create_tag(token=token, org_id=org, kind="generic", name="surf-tag")
    created = await create_task(token=token, org_id=org, title="tagged", tag_ids=[tag["id"]])

    full = await get_task(token=token, org_id=org, task_id=created["id"])
    names = {g["name"] for g in full["tags"]}
    assert "surf-tag" in names
    surf = next(g for g in full["tags"] if g["name"] == "surf-tag")
    assert {"id", "kind", "name", "color"} <= surf.keys()

    listed = next(t for t in await list_tasks(token=token, org_id=org) if t["id"] == created["id"])
    assert "surf-tag" in {g["name"] for g in listed["tags"]}
