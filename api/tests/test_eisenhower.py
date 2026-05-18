"""Eisenhower priority: importance x urgency persisted, priority
derived (1 = highest, ADR-0004), and it round-trips."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_priority_derived_from_importance_urgency() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}

        # importance/urgency: 1 = most pressing (Critical/Now),
        # 5 = least (Trivial/Whenever). priority = importance*urgency.
        # 5 x 5 = 25 -> least prioritary.
        t1 = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "trivial", "importance": 5, "urgency": 5},
            )
        ).json()
        assert t1["priority"] == 25
        assert t1["importance"] == 5
        assert t1["urgency"] == 5

        # 1 x 1 = 1 -> most prioritary (Critical + Now)
        t2 = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "crit", "importance": 1, "urgency": 1},
            )
        ).json()
        assert t2["priority"] == 1

        # patch importance/urgency -> priority re-derived (3 x 3 = 9)
        r = await c.patch(
            f"/tasks/{t1['id']}",
            headers=h,
            json={"expected_version": 1, "importance": 3, "urgency": 3},
        )
        assert r.status_code == 200
        got = (await c.get(f"/tasks/{t1['id']}", headers=h)).json()
        assert got["importance"] == 3
        assert got["urgency"] == 3
        assert got["priority"] == 9

        # legacy path: explicit priority, no importance/urgency
        t3 = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "legacy", "priority": 2},
            )
        ).json()
        assert t3["priority"] == 2
        assert t3["importance"] is None
        assert t3["urgency"] is None
