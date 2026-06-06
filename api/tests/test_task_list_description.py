"""Perf: the task LIST endpoint defers ``description`` (so listing hundreds
of tasks doesn't transfer every body); the DETAIL endpoint still returns it.
The SPA free-text search is server-side and the body is edited on the detail
page, so the list never needs it.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_list_defers_description_detail_keeps_it() -> None:
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
        }
        tk = (
            await c.post(
                "/tasks", headers=h, json={"title": "T", "description": "the body"}
            )
        ).json()
        tid = tk["id"]

        # LIST: description is deferred -> not shipped (None).
        row = next(t for t in (await c.get("/tasks", headers=h)).json() if t["id"] == tid)
        assert row["description"] is None

        # DETAIL: the full description is returned.
        detail = (await c.get(f"/tasks/{tid}", headers=h)).json()
        assert detail["description"] == "the body"
