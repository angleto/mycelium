"""Editable time entries + resolved entry context + per-task report.

A user can correct an entry: reassign it to another task
(transitively re-deriving project/client), and fix the recorded
interval (duration is recomputed). TimeEntryOut carries the resolved
task/project/client + the client's IANA timezone so the list / report
need no N+1. The per-task report aggregates total/billable/count.

Time + task operations are member-level; client writes are
owner-gated, so the acting role is ``owner`` (clamped to the
membership; a fresh signup's caller is the workspace owner)."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_time_edit_reassign_interval_context_and_report() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        # Two clients (one with a timezone), one project each.
        cli_a = (
            await c.post(
                "/clients",
                headers=h,
                json={
                    "name": "Client A",
                    "legal_name": "Client A srl",
                    "timezone": "Europe/Rome",
                },
            )
        ).json()
        cli_b = (
            await c.post(
                "/clients",
                headers=h,
                json={"name": "Client B", "legal_name": "Client B srl"},
            )
        ).json()
        # Client create echoes nothing of the profile (TagOut); read it
        # back from the clients list to assert the timezone persisted.
        clients = (await c.get("/clients", headers=h)).json()
        by_id = {x["id"]: x for x in clients}
        assert by_id[cli_a["id"]]["timezone"] == "Europe/Rome"
        assert by_id[cli_b["id"]]["timezone"] is None

        proj_a = (
            await c.post(
                "/projects",
                headers=h,
                json={"name": "Proj A", "client_tag_id": cli_a["id"]},
            )
        ).json()
        proj_b = (
            await c.post(
                "/projects",
                headers=h,
                json={"name": "Proj B", "client_tag_id": cli_b["id"]},
            )
        ).json()

        task_a = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Task A", "tag_ids": [proj_a["id"]]},
            )
        ).json()
        task_b = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Task B", "tag_ids": [proj_b["id"]]},
            )
        ).json()

        # Start + stop a serial timer on task A.
        await c.post("/time/start", headers=h, json={"task_id": task_a["id"]})
        stopped = (await c.post("/time/stop", headers=h, json={})).json()
        entry_id = stopped["id"]
        assert stopped["task_id"] == task_a["id"]
        # Resolved context surfaces on the stopped entry (no N+1).
        assert stopped["task_title"] == "Task A"
        assert stopped["project_tag_id"] == proj_a["id"]
        assert stopped["project_name"] == "Proj A"
        assert stopped["client_tag_id"] == cli_a["id"]
        assert stopped["client_name"] == "Client A"
        assert stopped["client_timezone"] == "Europe/Rome"
        assert stopped["duration_seconds"] is not None

        # --- Reassign the entry to task B (-> B's project/client). ---
        r = await c.patch(
            f"/time/entries/{entry_id}",
            headers=h,
            json={"expected_version": stopped["version"], "task_id": task_b["id"]},
        )
        assert r.status_code == 200, r.text
        v2 = r.json()["version"]
        assert v2 == stopped["version"] + 1
        ent = (await c.get(f"/time/entries/{entry_id}", headers=h)).json()
        assert ent["task_id"] == task_b["id"]
        assert ent["project_tag_id"] == proj_b["id"]
        assert ent["project_name"] == "Proj B"
        assert ent["client_tag_id"] == cli_b["id"]
        assert ent["client_name"] == "Client B"
        assert ent["client_timezone"] is None  # Client B has no tz

        # --- Adjust the interval -> duration recomputed. ---
        r = await c.patch(
            f"/time/entries/{entry_id}",
            headers=h,
            json={
                "expected_version": v2,
                "started_at": "2026-06-01T09:00:00+00:00",
                "ended_at": "2026-06-01T11:30:00+00:00",
            },
        )
        assert r.status_code == 200, r.text
        v3 = r.json()["version"]
        ent = (await c.get(f"/time/entries/{entry_id}", headers=h)).json()
        assert ent["duration_seconds"] == 9000  # 2h30m

        # --- Invalid interval (ended_at <= started_at) -> 400. ---
        r = await c.patch(
            f"/time/entries/{entry_id}",
            headers=h,
            json={
                "expected_version": v3,
                "started_at": "2026-06-01T12:00:00+00:00",
                "ended_at": "2026-06-01T12:00:00+00:00",
            },
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "time_entry.invalid"

        # --- Reassign to a non-existent task -> 404 TASK_NOT_FOUND. ---
        r = await c.patch(
            f"/time/entries/{entry_id}",
            headers=h,
            json={"expected_version": v3, "task_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "task.not_found"

        # --- A second entry on task B; per-task report aggregates. ---
        m = await c.post(
            "/time/entries",
            headers=h,
            json={
                "task_id": task_b["id"],
                "started_at": "2026-06-02T09:00:00+00:00",
                "ended_at": "2026-06-02T09:30:00+00:00",
                "billable": False,
            },
        )
        assert m.status_code == 200, m.text

        rep = (await c.get("/time/report/by-task", headers=h)).json()
        rows = {r["task_id"]: r for r in rep}
        assert task_b["id"] in rows
        row = rows[task_b["id"]]
        # Entry 1 (reassigned, 9000s, billable) + entry 2 (1800s,
        # non-billable) -> 10800 total, 9000 billable, 2 entries.
        assert row["total_seconds"] == 10800
        assert row["billable_seconds"] == 9000
        assert row["entry_count"] == 2
        # Report carries the resolved client/project + timezone.
        assert row["task_title"] == "Task B"
        assert row["project_tag_id"] == proj_b["id"]
        assert row["project_name"] == "Proj B"
        assert row["client_tag_id"] == cli_b["id"]
        assert row["client_name"] == "Client B"
        assert row["client_timezone"] is None

        # Set Client B's timezone via the owner-gated client patch and
        # assert it now surfaces in TimeEntryOut + the per-task report.
        cli_b_full = next(
            x for x in (await c.get("/clients", headers=h)).json() if x["id"] == cli_b["id"]
        )
        pr = await c.patch(
            f"/clients/{cli_b['id']}",
            headers=h,
            json={"timezone": "America/New_York"},
        )
        assert pr.status_code == 204, pr.text
        assert cli_b_full["timezone"] is None  # was unset before the patch
        ent = (await c.get(f"/time/entries/{entry_id}", headers=h)).json()
        assert ent["client_timezone"] == "America/New_York"
        rep = (await c.get("/time/report/by-task", headers=h)).json()
        row = next(r for r in rep if r["task_id"] == task_b["id"])
        assert row["client_timezone"] == "America/New_York"

        # Report is ordered by total_seconds desc: task B (10800) is
        # the only task with entries here, and leads.
        assert rep[0]["task_id"] == task_b["id"]


async def test_delete_time_entry_stopped_running_and_isolation() -> None:
    """DELETE /time/entries/{id} (member-level): a stopped entry is
    removed (-> 404 on a later GET); deleting a STILL-RUNNING entry
    cancels it (it is gone); a foreign entry id is 404 (cross-org)."""
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
        ha = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
        hb = {"Authorization": f"Bearer {b['token']}", "X-Workspace-Id": b["workspace_id"]}

        task = (await c.post("/tasks", headers=ha, json={"title": "T"})).json()

        # Stopped entry -> delete -> 404 afterwards.
        await c.post("/time/start", headers=ha, json={"task_id": task["id"]})
        stopped = (await c.post("/time/stop", headers=ha, json={})).json()
        eid = stopped["id"]
        d = await c.delete(f"/time/entries/{eid}", headers=ha)
        assert d.status_code == 204, d.text
        gone = await c.get(f"/time/entries/{eid}", headers=ha)
        assert gone.status_code == 404, gone.text
        assert gone.json()["code"] == "time_entry.not_found"

        # Still-running entry -> delete cancels it (gone, no running).
        run = (await c.post("/time/start", headers=ha, json={"task_id": task["id"]})).json()
        rid = run["id"]
        assert run["ended_at"] is None
        dr = await c.delete(f"/time/entries/{rid}", headers=ha)
        assert dr.status_code == 204, dr.text
        assert (await c.get(f"/time/entries/{rid}", headers=ha)).status_code == 404
        assert (await c.get("/time/running", headers=ha)).json() == []

        # Cross-org: org B cannot delete org A's entry (RLS -> 404).
        await c.post("/time/start", headers=ha, json={"task_id": task["id"]})
        a_entry = (await c.post("/time/stop", headers=ha, json={})).json()["id"]
        foreign = await c.delete(f"/time/entries/{a_entry}", headers=hb)
        assert foreign.status_code == 404, foreign.text
        # Still intact for org A.
        assert (await c.get(f"/time/entries/{a_entry}", headers=ha)).status_code == 200
