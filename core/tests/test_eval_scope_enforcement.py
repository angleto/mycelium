"""The WS-EVAL harness exercises the per-tool scope gate (task c19f2f63).

eval_scenarios.ScenarioRunner.call used to publish only ``_PRINCIPAL``, so every
scenario ran full-access and the scope enforcement (enabler B) was invisible to
the eval. It now publishes ``_PRINCIPAL_SCOPE`` too, from the actor's scope (an
explicit ``ScenarioActor.scope`` or the bound assistant's stored scope), so a
scenario can assert a restricted actor is denied an out-of-scope tool.
"""

from __future__ import annotations

import uuid

import mycelium_mcp.eval_scenarios as sc
from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup


async def _owner() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="EVALSCOPE",
        )
    return r.org_id, r.user_id


async def test_harness_enforces_actor_scope() -> None:
    org, user = await _owner()
    runner = sc.ScenarioRunner(org_id=org)

    # A read-only actor: tasks:read granted, tasks:write refused.
    reader = sc.ScenarioActor(
        name="reader", kind="agent", user_id=user, org_id=org, scope=["tasks:read"]
    )

    # In scope: list_tasks runs (the gate lets tasks:read through).
    listed = await runner.call(reader, "list_tasks", {})
    assert not (isinstance(listed, dict) and "error" in listed), listed

    # Out of scope: the gateway gate denies BEFORE dispatch -- the exact property
    # that was untestable before this fix.
    denied = await runner.call(reader, "create_task", {"title": "nope"})
    assert isinstance(denied, dict) and denied["error"]["code"] == "mcp.scope_denied"
    assert runner.steps[-1].error_code == "mcp.scope_denied"
    assert runner.steps[-1].ok is False

    # A full-access actor (scope=None, the legacy default) is unchanged.
    full = sc.ScenarioActor(name="full", kind="agent", user_id=user, org_id=org)
    created = await runner.call(full, "create_task", {"title": "allowed"})
    assert not (isinstance(created, dict) and "error" in created), created
