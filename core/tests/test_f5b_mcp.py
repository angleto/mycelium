"""F5b MCP co-equality (DB-backed): billing tools reuse the same
service layer as REST (docs/adr/0001), incl. idempotent metering."""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    billing_balance,
    grant_credits,
    meter_usage,
    upsert_rate_card,
)


async def test_mcp_billing() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP5B",
        )
    token, org = r.token, str(r.org_id)

    await grant_credits(token=token, org_id=org, amount=50.0, reason="seed")
    assert (await billing_balance(token=token, org_id=org))["balance"] == "50.0000"

    await upsert_rate_card(
        token=token,
        org_id=org,
        model_id="m1",
        provider="local",
        credits_per_input=3.0,
    )
    u1 = await meter_usage(
        token=token,
        org_id=org,
        operation_id="op-1",
        op="llm",
        model_id="m1",
        units_in=4.0,
    )
    u2 = await meter_usage(
        token=token,
        org_id=org,
        operation_id="op-1",
        op="llm",
        model_id="m1",
        units_in=4.0,
    )
    assert u1["credits"] == "12.0000"
    assert u1["id"] == u2["id"]  # idempotent, charged once
    assert (await billing_balance(token=token, org_id=org))["balance"] == "38.0000"
