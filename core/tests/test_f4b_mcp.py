"""F4b MCP co-equality (DB-backed): budgets + deterministic advisory
reuse the same service layer as REST (docs/adr/0001)."""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    create_budget,
    create_task,
    prioritize_within_budget,
    what_can_i_do_now,
)


async def test_mcp_budget_and_advisory() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP4B",
        )
    token, org, me = r.token, str(r.org_id), str(r.user_id)

    bud = await create_budget(
        token=token,
        org_id=org,
        name="Home",
        period_kind="month",
        period_start="2026-01-01",
        period_end="2026-01-31",
        amount=80.0,
    )
    must = await create_task(
        token=token,
        org_id=org,
        title="must-buy",
        importance=1,
        urgency=1,
        monetary_cost=50.0,
        necessity="must",
        budget_id=bud["id"],
        estimate_effort_h=0.5,
        assignee_ids=[me],
    )
    await create_task(
        token=token,
        org_id=org,
        title="too-expensive",
        importance=1,
        urgency=2,
        monetary_cost=60.0,
        necessity="should",
        budget_id=bud["id"],
    )
    plan = await prioritize_within_budget(token=token, org_id=org, budget_id=bud["id"])
    assert [p["task_id"] for p in plan["selected"]] == [must["id"]]
    assert plan["allocated"] == "50.00"
    assert plan["excluded"][0]["reason"] == "budget_exhausted"

    feasible = await what_can_i_do_now(
        token=token,
        org_id=org,
        window_start="2026-01-12T09:00:00+00:00",
        duration_minutes=60,
    )
    # T6: bare list -> NarratedPlanOut envelope (REST parity).
    assert feasible["narrated"] is False and feasible["narration"] is None
    assert must["id"] in {x["task_id"] for x in feasible["ranked"]}


async def test_mcp_what_now_envelope_default_now_and_selection() -> None:
    """T6: MCP mirrors REST -- optional window_start (default now()),
    selection params, slack/bucket on rows, NarratedPlanOut envelope."""
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP4B-SEL",
        )
    token, org, me = r.token, str(r.org_id), str(r.user_id)

    hi = await create_task(
        token=token,
        org_id=org,
        title="hi-must",
        importance=1,
        urgency=1,
        necessity="must",
        estimate_effort_h=0.5,
        assignee_ids=[me],
    )  # priority 1, must
    lo = await create_task(
        token=token,
        org_id=org,
        title="lo-could",
        importance=3,
        urgency=3,
        necessity="could",
        estimate_effort_h=0.5,
        assignee_ids=[me],
    )  # priority 9, could

    # window_start omitted -> server now(); full deterministic envelope.
    env = await what_can_i_do_now(token=token, org_id=org, duration_minutes=60)
    assert env["narrated"] is False and env["narration"] is None
    assert {x["task_id"] for x in env["ranked"]} == {hi["id"], lo["id"]}
    row = next(x for x in env["ranked"] if x["task_id"] == hi["id"])
    assert "slack_minutes" in row and "deadline_bucket" in row

    # min_priority (importance floor) narrows to the priority<=5 task.
    sel = await what_can_i_do_now(token=token, org_id=org, duration_minutes=60, min_priority=5)
    assert {x["task_id"] for x in sel["ranked"]} == {hi["id"]}

    # min_necessity coercion (str -> Necessity): the 'could' task drops.
    nec = await what_can_i_do_now(
        token=token, org_id=org, duration_minutes=60, min_necessity="should"
    )
    assert {x["task_id"] for x in nec["ranked"]} == {hi["id"]}
