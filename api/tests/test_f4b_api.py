"""F4b API end-to-end (DB-backed): budget envelope + consumption,
deterministic advisory (what-now, errands, budget plan), cross-org
isolation."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f4b_api_flow() -> None:
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
        me = a["user_id"]

        bud = (
            await c.post(
                "/budgets",
                headers=h,
                json={
                    "name": "Home",
                    "period_kind": "month",
                    "period_start": "2026-01-01",
                    "period_end": "2026-01-31",
                    "amount": "100",
                },
            )
        ).json()

        must = (
            await c.post(
                "/tasks",
                headers=h,
                json={
                    "title": "fix boiler",
                    "priority": 1,
                    "estimate_effort_h": "0.5",
                    "monetary_cost": "60",
                    "necessity": "must",
                    "budget_id": bud["id"],
                    "location": "home",
                    "assignee_ids": [me],
                },
            )
        ).json()
        assert must["necessity"] == "must" and must["budget_id"] == bud["id"]
        await c.post(
            "/tasks",
            headers=h,
            json={
                "title": "nice lamp",
                "priority": 3,
                "estimate_effort_h": "0.5",
                "monetary_cost": "70",
                "necessity": "nice",
                "budget_id": bud["id"],
                "assignee_ids": [me],
            },
        )

        cons = (await c.get(f"/budgets/{bud['id']}/consumption", headers=h)).json()
        assert cons["consumed"] == "130.00" and cons["residual"] == "-30.00"

        plan = (await c.get(f"/advisory/budget/{bud['id']}/plan", headers=h)).json()
        # The must-have (60) fits; the nice (70) does not within 100.
        assert [p["task_id"] for p in plan["selected"]] == [must["id"]]
        assert plan["allocated"] == "60.00"
        assert plan["excluded"][0]["reason"] == "budget_exhausted"

        now = await c.post(
            "/advisory/what-now",
            headers=h,
            json={"window_start": "2026-01-12T09:00:00+00:00", "duration_minutes": 60},
        )
        assert now.status_code == 200
        assert must["id"] in {x["task_id"] for x in now.json()}

        err = await c.post("/advisory/errands", headers=h, json={"location": "home"})
        assert err.status_code == 200
        assert {x["task_id"] for x in err.json()} == {must["id"]}

        cross = await c.get(
            "/budgets",
            headers={"Authorization": f"Bearer {a['token']}", "X-Org-Id": b["org_id"]},
        )
        assert cross.status_code == 403
