"""F3 MCP co-equality (DB-backed): events (no-ubiquity) and the
deterministic schedule reuse the same service layer as REST
(docs/adr/0001)."""

from __future__ import annotations

import uuid

import pytest

from flow_core.db import admin_session
from flow_core.errors import DomainError
from flow_core.services.auth import signup
from flow_mcp.server import (
    create_event,
    create_task,
    list_schedule,
    recompute_schedule,
    set_task_schedule,
)


async def test_mcp_events_and_schedule() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP3",
        )
    token, org, me = r.token, str(r.org_id), str(r.user_id)

    await create_event(
        token=token,
        org_id=org,
        title="Standup",
        start_at="2026-01-12T09:00:00+00:00",
        end_at="2026-01-12T09:30:00+00:00",
        participant_ids=[me],
    )
    with pytest.raises(DomainError):
        await create_event(
            token=token,
            org_id=org,
            title="Clash",
            start_at="2026-01-12T09:15:00+00:00",
            end_at="2026-01-12T09:45:00+00:00",
            participant_ids=[me],
        )

    t = await create_task(token=token, org_id=org, title="MCP task")
    await set_task_schedule(
        token=token,
        org_id=org,
        task_id=t["id"],
        expected_version=t["version"],
        remaining_effort_h=2.0,
    )
    out = await recompute_schedule(token=token, org_id=org, as_of="2026-01-12T08:00:00+00:00")
    assert out["count"] == 1
    rows = await list_schedule(token=token, org_id=org)
    assert len(rows) == 1 and rows[0]["task_id"] == t["id"]
    assert rows[0]["scheduled_start"] is not None
