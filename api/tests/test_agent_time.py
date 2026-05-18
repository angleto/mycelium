"""AI-tracked time is distinguishable and never summed into a human's
totals; TaskOut exposes the estimate field."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_executor_kind_snapshot_and_report_split() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}

        human = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "mine", "estimate_effort_h": "2.5"},
            )
        ).json()
        assert human["executor_kind"] == "human"
        assert human["estimate_effort_h"] is not None

        ai = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "ai", "executor_kind": "llm_agent"},
            )
        ).json()
        assert ai["executor_kind"] == "llm_agent"

        # A manual entry on each task snapshots the executor.
        for tid in (human["id"], ai["id"]):
            r = await c.post(
                "/time/entries",
                headers=h,
                json={
                    "task_id": tid,
                    "started_at": "2026-06-01T09:00:00",
                    "duration_seconds": 3600,
                },
            )
            assert r.status_code == 200, r.text
        entries = (await c.get("/time/entries", headers=h)).json()
        by_task = {e["task_id"]: e for e in entries}
        assert by_task[human["id"]]["executor_kind"] == "human"
        assert by_task[ai["id"]]["executor_kind"] == "llm_agent"

        # Report split: human-only excludes the AI hour and vice versa.
        rep_h = (
            await c.get(
                "/time/report",
                headers=h,
                params={"group_by": "user", "executor_kind": "human"},
            )
        ).json()
        rep_ai = (
            await c.get(
                "/time/report",
                headers=h,
                params={"group_by": "user", "executor_kind": "llm_agent"},
            )
        ).json()
        sec_h = sum(r["seconds"] for r in rep_h)
        sec_ai = sum(r["seconds"] for r in rep_ai)
        assert sec_h == 3600
        assert sec_ai == 3600  # distinct, not summed into the human total
