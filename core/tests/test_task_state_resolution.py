"""Tasks expose the resolved workflow STATE name over MCP (task c19f2f63).

get_task / list_tasks / create_task used to return only ``state_id`` (a bare
uuid), so an agent could not read a task's state without a separate lookup and
would sometimes infer the state set from existing tasks. They now resolve the
state name (+ is_terminal + workflow_id) alongside the id, in one batch query.
"""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup
from mycelium_mcp.server import create_task, get_task, list_tasks, workflow_states


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_tasks_expose_resolved_state_over_mcp() -> None:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="STATE")
    assert r.token is not None
    token, org = r.token, str(r.org_id)

    created = await create_task(token=token, org_id=org, title="triage me")
    tid = created["id"]
    # create_task return is enriched too.
    assert isinstance(created["state"], str) and created["state"]
    assert created["workflow_id"]

    full = await get_task(token=token, org_id=org, task_id=tid)
    assert isinstance(full["state"], str) and full["state"]  # a NAME, not the uuid
    assert full["state_id"] and full["workflow_id"]
    assert full["state_is_terminal"] is False  # a fresh task starts non-terminal

    # The resolved name is the real state, cross-checked against workflow_states.
    states = await workflow_states(token=token, org_id=org, workflow_id=full["workflow_id"])
    by_id = {st["id"]: st for st in states}
    assert by_id[full["state_id"]]["name"] == full["state"]

    # A list row carries the same resolved state.
    page = await list_tasks(token=token, org_id=org)
    row = next(x for x in page["items"] if x["id"] == tid)
    assert row["state"] == full["state"]
    assert row["workflow_id"] == full["workflow_id"]
    assert row["state_is_terminal"] is False
