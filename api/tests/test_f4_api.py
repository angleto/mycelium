"""F4 API end-to-end (DB-backed): timer lifecycle (single running
timer), manual entry, list/patch/delete, report + CSV export,
cross-org isolation."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f4_api_flow() -> None:
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

        proj = (await c.post("/projects", headers=h, json={"name": "P", "tariffa": "120"})).json()
        t = (await c.post("/tasks", headers=h, json={"title": "T", "tag_ids": [proj["id"]]})).json()

        started = await c.post("/time/start", headers=h, json={"task_id": t["id"]})
        assert started.status_code == 200 and started.json()["ended_at"] is None
        dup = await c.post("/time/start", headers=h, json={"task_id": t["id"]})
        assert dup.status_code == 400
        assert dup.json()["code"] == "time.timer_already_running"
        assert (await c.get("/time/running", headers=h)).json()["id"] == started.json()["id"]

        stopped = await c.post("/time/stop", headers=h, json={})
        assert stopped.status_code == 200 and stopped.json()["ended_at"] is not None
        assert (await c.get("/time/running", headers=h)).json() is None

        manual = await c.post(
            "/time/entries",
            headers=h,
            json={
                "task_id": t["id"],
                "started_at": "2026-01-12T09:00:00+00:00",
                "duration_seconds": 3600,
                "billable": True,
            },
        )
        assert manual.status_code == 200
        me = manual.json()
        assert me["rate_snapshot"] == "120.00" and me["duration_seconds"] == 3600

        entries = (await c.get("/time/entries", headers=h)).json()
        assert len(entries) == 2

        patched = await c.patch(
            f"/time/entries/{me['id']}",
            headers=h,
            json={"expected_version": me["version"], "billable": False},
        )
        assert patched.status_code == 200 and patched.json()["version"] == me["version"] + 1

        rep = (await c.get("/time/report?group_by=project", headers=h)).json()
        prow = next(r for r in rep if r["key"] == proj["id"])
        assert prow["seconds"] == 3600 and prow["billable_seconds"] == 0  # now non-billable

        csv_resp = await c.get("/time/report.csv?group_by=project", headers=h)
        assert csv_resp.status_code == 200
        assert csv_resp.headers["content-type"].startswith("text/csv")
        assert "key,label,seconds,billable_seconds,amount,currency" in csv_resp.text

        assert (await c.delete(f"/time/entries/{me['id']}", headers=h)).status_code == 204
        assert len((await c.get("/time/entries", headers=h)).json()) == 1

        cross = await c.get(
            "/time/entries",
            headers={"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": b["workspace_id"]},
        )
        assert cross.status_code == 403
