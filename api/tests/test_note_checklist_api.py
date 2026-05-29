"""Note checklist (task bae178d2): the same lightweight ticked-item
widget as tasks, attached to a note via the polymorphic owner. Items
may carry an optional markdown ``body`` (the "articulate comment",
opened / edited as markdown in the shared widget).

Mirrors test_task_checklist_api but on ``/notes/{id}/checklist``; also
asserts the owner is the note (not a task) and the body round-trips.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "N"},
        )
    ).json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def test_note_checklist_crud_body_and_reorder() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note = (await c.post("/notes", headers=h, json={"kind": "text", "text": "Plan"})).json()
        nid = note["id"]

        # Empty list initially.
        assert (await c.get(f"/notes/{nid}/checklist", headers=h)).json() == []

        # Add three items; the second carries a markdown body comment.
        labels = ["draft outline", "review with team", "publish"]
        created = []
        for i, label in enumerate(labels):
            payload = {"text": label}
            if i == 1:
                payload["body"] = "## Notes\n- ask Bob\n- check the **deadline**"
            r = await c.post(f"/notes/{nid}/checklist", headers=h, json=payload)
            assert r.status_code == 201, r.text
            created.append(r.json())

        # Owner is the note, not a task; body round-trips.
        assert all(it["note_id"] == nid and it["task_id"] is None for it in created)
        assert created[1]["body"].startswith("## Notes")
        assert created[0]["body"] is None
        positions = [int(it["position"]) for it in created]
        assert positions == sorted(positions) and len(set(positions)) == 3

        # Toggle one done + edit its body; stamps done + version bumps.
        first = created[0]
        r = await c.patch(
            f"/notes/{nid}/checklist/{first['id']}",
            headers=h,
            json={"expected_version": first["version"], "done": True, "body": "did it"},
        )
        assert r.status_code == 200, r.text
        patched = r.json()
        assert patched["done"] is True and patched["done_at"] is not None
        assert patched["body"] == "did it"
        assert patched["version"] == first["version"] + 1

        # Stale write -> 409.
        stale = await c.patch(
            f"/notes/{nid}/checklist/{first['id']}",
            headers=h,
            json={"expected_version": first["version"], "text": "late"},
        )
        assert stale.status_code == 409

        # Reorder (reverse) — full-set rewrite, returns new order.
        ids = [it["id"] for it in created]
        r = await c.post(
            f"/notes/{nid}/checklist:reorder",
            headers=h,
            json={"ids": list(reversed(ids))},
        )
        assert r.status_code == 200, r.text
        assert [it["id"] for it in r.json()] == list(reversed(ids))

        # clear_done drops the one marked done.
        r = await c.post(f"/notes/{nid}/checklist:clear_done", headers=h)
        assert r.status_code == 200 and r.json()["removed"] == 1
        remaining = (await c.get(f"/notes/{nid}/checklist", headers=h)).json()
        assert first["id"] not in {it["id"] for it in remaining}
        assert len(remaining) == 2

        # Delete one explicitly.
        r = await c.delete(f"/notes/{nid}/checklist/{remaining[0]['id']}", headers=h)
        assert r.status_code == 204
        assert len((await c.get(f"/notes/{nid}/checklist", headers=h)).json()) == 1


async def test_note_checklist_rejects_empty_text() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        nid = (await c.post("/notes", headers=h, json={"kind": "text", "text": "x"})).json()["id"]
        r = await c.post(f"/notes/{nid}/checklist", headers=h, json={"text": "   "})
        assert r.status_code in (400, 422)
