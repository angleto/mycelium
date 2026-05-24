"""F3 API end-to-end (DB-backed): calendars + holidays, events with
no-ubiquity rejection, deterministic schedule recompute/read, manual
pin write-back surviving recompute, cross-org isolation."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f3_api_flow() -> None:
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
        # Owner with full entitlement: privileged writes need the
        # effective role, X-Workspace-Role clamped to the membership
        # (absent header => member, least privilege).
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        me = a["user_id"]

        # Default calendar is provisioned with the org.
        cals = (await c.get("/calendars", headers=h)).json()
        assert len(cals) == 1 and cals[0]["is_default"] is True
        cal_id = cals[0]["id"]

        hol = await c.post(f"/calendars/{cal_id}/holidays", headers=h, json={"day": "2026-01-14"})
        assert hol.status_code == 204
        days = (await c.get(f"/calendars/{cal_id}/holidays", headers=h)).json()
        assert days == [{"day": "2026-01-14"}]
        assert (
            await c.delete(f"/calendars/{cal_id}/holidays/2026-01-14", headers=h)
        ).status_code == 204
        assert (await c.get(f"/calendars/{cal_id}/holidays", headers=h)).json() == []

        # Two human tasks, same assignee, no dependency.
        t1 = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "T1", "estimate_effort_h": "4", "assignee_ids": [me]},
            )
        ).json()
        t2 = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "T2", "estimate_effort_h": "4", "assignee_ids": [me]},
            )
        ).json()

        rec = await c.post(
            "/schedule/recompute",
            headers=h,
            json={"as_of": "2026-01-12T08:00:00+00:00"},
        )
        assert rec.status_code == 200 and rec.json()["count"] == 2

        sched = {s["task_id"]: s for s in (await c.get("/schedule", headers=h)).json()}
        assert set(sched) == {t1["id"], t2["id"]}
        one = (await c.get(f"/schedule/{t1['id']}", headers=h)).json()
        assert one["scheduled_start"] is not None
        # Serialized: T1 finishes no later than T2 starts.
        assert sched[t1["id"]]["scheduled_end"] <= sched[t2["id"]]["scheduled_start"]
        pinned = sched[t1["id"]]["scheduled_start"]

        # No-ubiquity: an identity cannot hold two overlapping
        # appointment-tasks (migration 0094, ADR-0008 addendum). The
        # legacy /events router is gone; create appointment-tasks via
        # /tasks with start_at + duration_minutes. The /tasks response
        # exposes ``assignee_id`` as the identity, so use that as the
        # axis for the conflict.
        from flow_core.db import tenant_session as _ts
        from flow_core.services import actors as _actors_svc
        from flow_core.services import identities as _identities_svc

        async with _ts(a["workspace_id"], a["user_id"]) as _s:
            await _actors_svc.mint_user_handle(_s, user_id=uuid.UUID(a["user_id"]), seed="f3")
            me_ident = (
                await _identities_svc.ensure_for_user(
                    _s,
                    org_id=uuid.UUID(a["workspace_id"]),
                    user_id=uuid.UUID(a["user_id"]),
                )
            ).id
        ev = await c.post(
            "/tasks",
            headers=h,
            json={
                "title": "Call",
                "start_at": "2026-01-12T09:00:00+00:00",
                "duration_minutes": 60,
                "assignee_id": str(me_ident),
                "executor_kind": "human",
                "necessity": "should",
                "priority": 3,
            },
        )
        assert ev.status_code == 200, ev.text
        clash = await c.post(
            "/tasks",
            headers=h,
            json={
                "title": "Overlap",
                "start_at": "2026-01-12T09:30:00+00:00",
                "duration_minutes": 60,
                "assignee_id": str(me_ident),
                "executor_kind": "human",
                "necessity": "should",
                "priority": 3,
            },
        )
        assert clash.status_code in (400, 409) and clash.json()["code"] == "event.overlap"

        # Drag write-back: pin T1 manual; an unrelated recompute keeps it.
        patched = await c.patch(
            f"/tasks/{t1['id']}/schedule",
            headers=h,
            json={"expected_version": t1["version"], "schedule_mode": "manual"},
        )
        assert patched.status_code == 200 and patched.json()["version"] == t1["version"] + 1
        await c.post(
            "/schedule/recompute",
            headers=h,
            json={"as_of": "2026-01-12T08:00:00+00:00"},
        )
        again = (await c.get(f"/schedule/{t1['id']}", headers=h)).json()
        assert again["scheduled_start"] == pinned

        # Cross-org isolation: A's token cannot read B's schedule.
        cross = await c.get(
            "/schedule",
            headers={"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": b["workspace_id"]},
        )
        assert cross.status_code == 403
