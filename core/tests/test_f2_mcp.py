"""F2 MCP co-equality (DB-backed): dependency + graph tools reuse the
same service layer (docs/adr/0001)."""

from __future__ import annotations

import uuid

import pytest

from flow_core.db import admin_session
from flow_core.errors import DomainError
from flow_core.services.auth import signup
from flow_mcp.server import add_dependency, create_task, graph


async def test_mcp_dependency_and_graph() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP2",
        )
    token, org = r.token, str(r.org_id)
    t1 = await create_task(token=token, org_id=org, title="A")
    t2 = await create_task(token=token, org_id=org, title="B")
    await add_dependency(
        token=token,
        org_id=org,
        predecessor_id=t1["id"],
        successor_id=t2["id"],
        type="FS",
    )
    with pytest.raises(DomainError):
        await add_dependency(
            token=token,
            org_id=org,
            predecessor_id=t2["id"],
            successor_id=t1["id"],
            type="FS",
        )
    g = await graph(token=token, org_id=org)
    assert len(g["edges"]) == 1 and g["edges"][0]["type"] == "FS"
