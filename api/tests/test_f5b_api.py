"""F5b API end-to-end (DB-backed): balance, admin grant, rate card,
idempotent metered debit, insufficient-credits rejection, cross-org
isolation."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f5b_api_flow() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "org_name": "A"},
            )
        ).json()
        b = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "org_name": "B"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Org-Id": a["org_id"]}

        assert (await c.get("/billing/balance", headers=h)).json()["balance"] == "0.0000"

        granted = await c.post("/billing/grant", headers=h, json={"amount": "100"})
        assert granted.status_code == 200 and granted.json()["balance"] == "100.0000"

        rc = await c.post(
            "/billing/rate-cards",
            headers=h,
            json={
                "model_id": "m1",
                "provider": "local",
                "credits_per_input": "2",
                "credits_per_output": "0",
            },
        )
        assert rc.status_code == 200 and rc.json()["model_id"] == "m1"

        body = {
            "operation_id": "op-1",
            "op": "llm",
            "model_id": "m1",
            "units_in": "5",
            "units_out": "0",
        }
        m1 = await c.post("/billing/meter", headers=h, json=body)
        assert m1.status_code == 200 and m1.json()["credits"] == "10.0000"
        m2 = await c.post("/billing/meter", headers=h, json=body)
        assert m2.json()["id"] == m1.json()["id"]  # idempotent
        assert (await c.get("/billing/balance", headers=h)).json()["balance"] == "90.0000"

        led = (await c.get("/billing/ledger", headers=h)).json()
        assert {e["kind"] for e in led} == {"grant", "debit"}

        broke = await c.post(
            "/billing/meter",
            headers=h,
            json={
                "operation_id": "op-2",
                "op": "llm",
                "model_id": "m1",
                "units_in": "1000",
            },
        )
        assert broke.status_code == 400
        assert broke.json()["code"] == "billing.insufficient_credits"

        cross = await c.get(
            "/billing/balance",
            headers={"Authorization": f"Bearer {a['token']}", "X-Org-Id": b["org_id"]},
        )
        assert cross.status_code == 403
