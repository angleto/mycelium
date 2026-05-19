"""Per-task reminders: CRUD + the scanner enqueues one notification
per configured offset to the task's assignees."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_reminders_crud_and_scan() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        uid = a["user_id"] if "user_id" in a else None
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
        }
        # whoami via a created task's assignee needs the user id; signup
        # returns it as `user_id` in this codebase's SignupOut.
        assert uid is not None

        tk = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Bill", "due_date": "2026-06-01"},
            )
        ).json()
        tid = tk["id"]

        # No reminders yet.
        assert (await c.get(f"/tasks/{tid}/reminders", headers=h)).json() == []

        # Add two (1 day before, at due). Dedup on same offset.
        r1 = await c.post(f"/tasks/{tid}/reminders", headers=h, json={"offset_minutes": 1440})
        assert r1.status_code == 200
        await c.post(f"/tasks/{tid}/reminders", headers=h, json={"offset_minutes": 0})
        dup = await c.post(f"/tasks/{tid}/reminders", headers=h, json={"offset_minutes": 1440})
        assert dup.status_code == 200
        rems = (await c.get(f"/tasks/{tid}/reminders", headers=h)).json()
        assert sorted(x["offset_minutes"] for x in rems) == [0, 1440]

        # Remove one.
        rid = r1.json()["id"]
        assert (await c.delete(f"/tasks/{tid}/reminders/{rid}", headers=h)).status_code == 204
        rems = (await c.get(f"/tasks/{tid}/reminders", headers=h)).json()
        assert [x["offset_minutes"] for x in rems] == [0]

        # Assign self + enable a channel, then scan: a reminder is queued.
        await c.post(f"/tasks/{tid}/assignees", headers=h, json={"user_id": uid})
        await c.put(
            "/notifications/prefs",
            headers=h,
            json={
                "user_id": uid,
                "channel": "email",
                "enabled": True,
                "target": "me@example.test",
            },
        )
        scan = await c.post("/notifications/reminders/scan?within_days=400", headers=h)
        assert scan.status_code == 200 and scan.json()["count"] >= 1
        notifs = (await c.get("/notifications", headers=h)).json()
        assert any(n["kind"] == "reminder" for n in notifs)
