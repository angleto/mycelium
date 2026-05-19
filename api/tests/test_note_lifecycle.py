"""Notes: edit (title auto-derive) + archive/delete/restore mirroring
tasks, with the list filters that back the trash & archive view.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_note_edit_archive_delete() -> None:
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
        n = (await c.post("/notes", headers=h, json={"kind": "text", "text": "first"})).json()
        nid = n["id"]

        # Edit with a blank title -> derived from the first body line.
        r = await c.patch(
            f"/notes/{nid}",
            headers=h,
            json={
                "expected_version": n["version"],
                "title": "",
                "text": "Pay the bill\nmore detail",
            },
        )
        assert r.status_code == 200
        cur = (await c.get(f"/notes/{nid}", headers=h)).json()
        assert cur["title"] == "Pay the bill"
        assert cur["transcript"].startswith("Pay the bill")

        # Archive -> out of the default list, visible with the flag.
        r = await c.post(
            f"/notes/{nid}/archive",
            headers=h,
            json={"expected_version": cur["version"]},
        )
        assert r.status_code == 200
        assert [x["id"] for x in (await c.get("/notes", headers=h)).json()] == []
        arch = (await c.get("/notes?include_archived=true", headers=h)).json()
        row = next(x for x in arch if x["id"] == nid)
        assert row["is_archived"] is True

        r = await c.post(
            f"/notes/{nid}/unarchive",
            headers=h,
            json={"expected_version": row["version"]},
        )
        assert r.status_code == 200
        cur = next(x for x in (await c.get("/notes", headers=h)).json() if x["id"] == nid)

        # Soft delete -> only with the flag; restore brings it back.
        r = await c.post(
            f"/notes/{nid}/delete",
            headers=h,
            json={"expected_version": cur["version"]},
        )
        assert r.status_code == 200
        assert [x["id"] for x in (await c.get("/notes", headers=h)).json()] == []
        deleted = (await c.get("/notes?include_deleted=true", headers=h)).json()
        drow = next(x for x in deleted if x["id"] == nid)
        assert drow["deleted_at"] is not None

        r = await c.post(
            f"/notes/{nid}/restore",
            headers=h,
            json={"expected_version": drow["version"]},
        )
        assert r.status_code == 200
        assert any(x["id"] == nid for x in (await c.get("/notes", headers=h)).json())
