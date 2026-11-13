"""The garden's operations reached through their generated MCP tools
(ADR-0068): the sensors that had no tool before, and the argument model
refusing a malformed call with a code instead of a traceback."""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import agent_tokens as at_svc
from mycelium_core.services.auth import signup
from mycelium_mcp.gateway import execute_tool
from mycelium_mcp.tools_garden import TOOLS


async def _token() -> str:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="GARDEN-OPS",
        )
    async with tenant_session(str(a.org_id), str(a.user_id)) as s:
        m = await at_svc.mint(s, org_id=a.org_id, actor_id=a.user_id, name="observer")
    return m.raw


async def test_a_malformed_id_is_refused_with_a_code_before_anything_runs() -> None:
    out = await execute_tool("garden_review_approve", {"note_id": "not-an-id"})
    err = out["error"]
    assert err["code"] == "operation.invalid_argument"
    assert "note_id" in err["detail"]
    assert err["params"]["operation"] == "garden.review.approve"


async def test_an_out_of_bounds_argument_is_refused_with_a_code() -> None:
    out = await execute_tool("garden_review_pending", {"limit": 0})
    assert out["error"]["code"] == "operation.invalid_argument"
    assert "limit" in out["error"]["detail"]


async def test_an_unknown_argument_is_still_refused_by_the_signature() -> None:
    out = await execute_tool("garden_health", {"verbose": True})
    assert out["error"]["code"] == "invalid_arguments"


async def test_the_sensors_answer_over_mcp() -> None:
    """Health, its history, the events behind it, the clusters, the
    per-model accept ratio and the learning telemetry were REST-only."""
    raw = await _token()

    health = await TOOLS["garden_health"](token=raw, org_id="")
    assert set(health) == {"generated_at", "metrics", "trend"}
    assert health["metrics"], "an empty workspace still reports its sensors"

    series = await TOOLS["garden_health_timeseries"](token=raw, org_id="", days=7)
    assert series == {"snapshots": []}

    events = await TOOLS["garden_health_events"](token=raw, org_id="")
    assert set(events) == {"events"}

    clusters = await TOOLS["garden_clusters"](token=raw, org_id="")
    assert set(clusters) == {"clusters", "modularity", "count"}

    ratio = await TOOLS["garden_review_accept_ratio"](token=raw, org_id="")
    assert ratio == {"models": []}

    learned = await TOOLS["garden_learning_telemetry"](token=raw, org_id="")
    assert learned == {"reject_hotspots": [], "drift": []}


async def test_a_rollback_with_nothing_learned_changes_nothing() -> None:
    raw = await _token()
    out = await TOOLS["garden_learning_rollback"](token=raw, org_id="", to="2026-01-01T00:00:00Z")
    assert out["features_changed"] == 0
    assert out["top_change"] is None
