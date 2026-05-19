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
        # Owner with full entitlement: taxonomy writes need the
        # effective role admin, which is X-Workspace-Role clamped to
        # the membership (absent header => member, least privilege).
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        # Rate is a client-level relationship now.
        cli = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "C",
                    "ragione_sociale": "C",
                    "tariffa": "120",
                    "default_billable": True,
                },
            )
        ).json()
        proj = (
            await c.post(
                "/projects",
                headers=h,
                json={"name": "P", "client_tag_id": cli["id"]},
            )
        ).json()
        t = (await c.post("/tasks", headers=h, json={"title": "T", "tag_ids": [proj["id"]]})).json()

        t2 = (await c.post("/tasks", headers=h, json={"title": "T2"})).json()

        started = await c.post("/time/start", headers=h, json={"task_id": t["id"]})
        assert started.status_code == 200 and started.json()["ended_at"] is None
        assert started.json()["parallel"] is False
        # Same task can't be double-tracked simultaneously.
        dup = await c.post("/time/start", headers=h, json={"task_id": t["id"], "parallel": True})
        assert dup.status_code == 400
        assert dup.json()["code"] == "time.timer_already_running"
        run = (await c.get("/time/running", headers=h)).json()
        assert [r["id"] for r in run] == [started.json()["id"]]

        # Serial start on another task stops the previous serial one.
        s2 = await c.post("/time/start", headers=h, json={"task_id": t2["id"]})
        assert s2.status_code == 200
        run = (await c.get("/time/running", headers=h)).json()
        assert [r["id"] for r in run] == [s2.json()["id"]]

        # Parallel timer runs alongside the serial one.
        par = await c.post("/time/start", headers=h, json={"task_id": t["id"], "parallel": True})
        assert par.status_code == 200 and par.json()["parallel"] is True
        assert len((await c.get("/time/running", headers=h)).json()) == 2

        # Stop a specific row by task; the serial one keeps running.
        st = await c.post("/time/stop", headers=h, json={"task_id": t["id"]})
        assert st.status_code == 200 and st.json()["ended_at"] is not None
        run = (await c.get("/time/running", headers=h)).json()
        assert [r["id"] for r in run] == [s2.json()["id"]]

        # No task -> stop the serial one; nothing left running.
        stopped = await c.post("/time/stop", headers=h, json={})
        assert stopped.status_code == 200 and stopped.json()["ended_at"] is not None
        assert (await c.get("/time/running", headers=h)).json() == []

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

        # e1 (auto-stopped), s2, par (stopped) + the manual entry.
        entries = (await c.get("/time/entries", headers=h)).json()
        assert len(entries) == 4

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
        assert len((await c.get("/time/entries", headers=h)).json()) == 3

        cross = await c.get(
            "/time/entries",
            headers={"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": b["workspace_id"]},
        )
        assert cross.status_code == 403
