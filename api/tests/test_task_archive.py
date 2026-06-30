"""Task archive/unarchive + soft-delete/restore endpoints and the
list filters that back the trash & archive view.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_archive_and_delete_lifecycle() -> None:
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
        tk = (await c.post("/tasks", headers=h, json={"title": "T"})).json()
        tid = tk["id"]

        # Archive: drops out of the default list, visible with the flag.
        r = await c.post(
            f"/tasks/{tid}/archive",
            headers=h,
            json={"expected_version": tk["version"]},
        )
        assert r.status_code == 200
        assert [t["id"] for t in (await c.get("/tasks", headers=h)).json()] == []
        arch = (await c.get("/tasks?include_archived=true", headers=h)).json()
        row = next(t for t in arch if t["id"] == tid)
        assert row["is_archived"] is True and row["deleted_at"] is None

        # Unarchive -> back in the default list.
        r = await c.post(
            f"/tasks/{tid}/unarchive",
            headers=h,
            json={"expected_version": row["version"]},
        )
        assert r.status_code == 200
        cur = next(t for t in (await c.get("/tasks", headers=h)).json() if t["id"] == tid)
        assert cur["is_archived"] is False

        # Soft delete -> deleted_at set, only visible with the flag.
        r = await c.post(
            f"/tasks/{tid}/delete",
            headers=h,
            json={"expected_version": cur["version"]},
        )
        assert r.status_code == 200
        assert [t["id"] for t in (await c.get("/tasks", headers=h)).json()] == []
        deleted = (await c.get("/tasks?include_deleted=true", headers=h)).json()
        drow = next(t for t in deleted if t["id"] == tid)
        assert drow["deleted_at"] is not None

        # Restore (undelete).
        r = await c.post(
            f"/tasks/{tid}/restore",
            headers=h,
            json={"expected_version": drow["version"]},
        )
        assert r.status_code == 200
        cur = next(t for t in (await c.get("/tasks", headers=h)).json() if t["id"] == tid)
        assert cur["deleted_at"] is None
