"""A task's "work note": POST /tasks/{id}/note opens (and on first
call creates) a single linked note. Idempotent; the note shows in the
notes list; ON DELETE SET NULL clears the link when the task is gone
(time stays task-scoped, no new model). An owner acting as a member is
fine: the endpoint is member-level.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from mycelium_api.main import app
from mycelium_core.db import tenant_session


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_task_work_note_lifecycle() -> None:
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

        tk = (await c.post("/tasks", headers=h, json={"title": "Write the spec"})).json()
        tid = tk["id"]

        # First call creates the work note.
        r = await c.post(f"/tasks/{tid}/note", headers=h)
        assert r.status_code == 200
        n = r.json()
        assert n["task_id"] == tid
        assert n["title"] == "Write the spec"
        assert n["kind"] == "text"
        # Every note belongs to a client: the task's project's client or
        # the default "Personal". A non-null client tag must be present.
        client_tags = [t for t in n["tags"] if t["kind"] == "client"]
        assert len(client_tags) == 1
        assert client_tags[0]["id"]

        # Idempotent: a second call returns the SAME note.
        r2 = await c.post(f"/tasks/{tid}/note", headers=h)
        assert r2.status_code == 200
        assert r2.json()["id"] == n["id"]

        # The work note appears in the notes list.
        lst = (await c.get("/notes", headers=h)).json()
        row = next(x for x in lst if x["id"] == n["id"])
        assert row["task_id"] == tid

        # Deleting the task clears the link (ON DELETE SET NULL): the
        # note survives, only task_id becomes NULL. The API only
        # soft-deletes tasks (deleted_at), which does not fire the FK
        # rule; hard-delete the task row directly (RLS-scoped session)
        # to exercise ON DELETE SET NULL, then re-fetch the note.
        async with tenant_session(a["workspace_id"], a["user_id"]) as s:
            await s.execute(text("DELETE FROM tasks WHERE id = :tid"), {"tid": tid})
        after = (await c.get(f"/notes/{n['id']}", headers=h)).json()
        assert after["id"] == n["id"]
        assert after["task_id"] is None
