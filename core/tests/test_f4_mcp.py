"""F4 MCP co-equality (DB-backed): the timer, manual entries and the
report reuse the same service layer as REST (docs/adr/0001)."""

from __future__ import annotations

import uuid

import pytest

from flow_core.db import admin_session
from flow_core.errors import DomainError
from flow_core.services.auth import signup
from flow_mcp.server import (
    add_time_entry,
    create_task,
    list_time_entries,
    start_timer,
    stop_timer,
    time_report,
)


async def test_mcp_time_tracking() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP4",
        )
    token, org = r.token, str(r.org_id)

    t = await create_task(token=token, org_id=org, title="MCP timed")
    e1 = await start_timer(token=token, org_id=org, task_id=t["id"])
    assert e1["ended_at"] is None
    with pytest.raises(DomainError):
        await start_timer(token=token, org_id=org, task_id=t["id"])
    stopped = await stop_timer(token=token, org_id=org)
    assert stopped["id"] == e1["id"] and stopped["ended_at"] is not None

    await add_time_entry(
        token=token,
        org_id=org,
        task_id=t["id"],
        started_at="2026-01-12T09:00:00+00:00",
        duration_seconds=1800,
    )
    rows = await list_time_entries(token=token, org_id=org)
    assert len(rows) == 2

    rep = await time_report(token=token, org_id=org, group_by="task")
    trow = next(r for r in rep if r["key"] == t["id"])
    assert trow["seconds"] >= 1800
