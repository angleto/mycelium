"""F4b API end-to-end (DB-backed): budget envelope + consumption,
deterministic advisory (what-now, errands, budget plan), cross-org
isolation."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f4b_api_flow() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
            )
        ).json()
        b = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "B"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
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
                    "importance": 1,
                    "urgency": 1,
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
                "title": "could lamp",
                "importance": 2,
                "urgency": 2,
                "estimate_effort_h": "0.5",
                "monetary_cost": "70",
                "necessity": "could",
                "budget_id": bud["id"],
                "assignee_ids": [me],
            },
        )

        cons = (await c.get(f"/budgets/{bud['id']}/consumption", headers=h)).json()
        assert cons["consumed"] == "130.00" and cons["residual"] == "-30.00"

        plan = (await c.get(f"/advisory/budget/{bud['id']}/plan", headers=h)).json()
        # The must (60) fits; the could (70) does not within 100.
        assert [p["task_id"] for p in plan["selected"]] == [must["id"]]
        assert plan["allocated"] == "60.00"
        assert plan["excluded"][0]["reason"] == "budget_exhausted"

        now = await c.post(
            "/advisory/what-now",
            headers=h,
            json={"window_start": "2026-01-12T09:00:00+00:00", "duration_minutes": 60},
        )
        assert now.status_code == 200
        now_body = now.json()
        # Breaking shape change (T4): bare list -> NarratedPlanOut envelope.
        assert now_body["narrated"] is False and now_body["narration"] is None
        ranked = now_body["ranked"]
        must_row = next(x for x in ranked if x["task_id"] == must["id"])
        # Deterministic deadline signal present on every ranked item.
        assert "slack_minutes" in must_row and "deadline_bucket" in must_row

        err = await c.post("/advisory/errands", headers=h, json={"location": "home"})
        assert err.status_code == 200
        assert {x["task_id"] for x in err.json()} == {must["id"]}

        cross = await c.get(
            "/budgets",
            headers={"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": b["workspace_id"]},
        )
        assert cross.status_code == 403


async def test_what_now_envelope_default_now_and_selection() -> None:
    """T4: window_start optional (server now()), naive value coerced (no
    500), selection params reach the core, NarratedPlanOut envelope."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "WN"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
        me = a["user_id"]
        tag_r = await c.post("/tags", headers=h, json={"kind": "generic", "name": "energy"})
        assert tag_r.status_code == 200, tag_r.text
        gtag = tag_r.json()["id"]

        async def mk(title: str, imp: int, urg: int, tags: list[str]) -> str:
            r = await c.post(
                "/tasks",
                headers=h,
                json={
                    "title": title,
                    "importance": imp,
                    "urgency": urg,
                    "estimate_effort_h": "0.5",
                    "assignee_ids": [me],
                    "tag_ids": tags,
                },
            )
            return r.json()["id"]

        tagged = await mk("tagged-prio9", 3, 3, [gtag])  # any-tag match, priority 9
        cheap = await mk("cheap-prio4", 2, 2, [])  # priority 4, untagged
        neither = await mk("neither-prio9", 3, 3, [])  # priority 9, untagged

        # window_start omitted -> 200 (not 422), ranked against server now().
        d = await c.post("/advisory/what-now", headers=h, json={"duration_minutes": 60})
        assert d.status_code == 200
        body = d.json()
        assert set(body.keys()) >= {"ranked", "narrated", "narration", "narration_model"}
        assert body["narrated"] is False
        assert {x["task_id"] for x in body["ranked"]} == {tagged, cheap, neither}

        # A naive window_start (no offset) must not 500.
        naive = await c.post(
            "/advisory/what-now",
            headers=h,
            json={"window_start": "2026-01-12T09:00:00", "duration_minutes": 60},
        )
        assert naive.status_code == 200

        # Selection UNION at the edge (no focus): tag-selection OR
        # min_priority. tagged matches the tag (priority 9), cheap matches
        # min_priority<=5; neither matches nothing and drops out.
        sel = await c.post(
            "/advisory/what-now",
            headers=h,
            json={
                "duration_minutes": 60,
                "any_tag_ids": [gtag],
                "min_priority": 5,
            },
        )
        assert sel.status_code == 200
        assert {x["task_id"] for x in sel.json()["ranked"]} == {tagged, cheap}
