"""Task checklist: lightweight ticked items inside a task.

Exercises the dedicated sub-resource (``/tasks/{id}/checklist`` and
the per-item endpoints). Verifies that the items live on a stable id
(not text patches), are embedded in ``GET /tasks/{id}``, honour
optimistic concurrency on per-item updates, and are not exposed via
``PATCH /tasks/{id}`` (no way to silently overwrite the checklist
through a generic task patch).
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient, name: str) -> dict[str, str]:
    r = await c.post(
        "/auth/signup",
        json={"email": _email(), "password": "pw-strong-123", "workspace_name": name},
    )
    return r.json()


async def test_checklist_crud_and_concurrency() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = await _signup(c, "A")
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        task = (await c.post("/tasks", headers=h, json={"title": "Shopping"})).json()
        tid = task["id"]
        # A freshly created task carries an empty checklist embedded.
        assert task.get("checklist") == []

        # Empty / whitespace text is rejected by the pydantic schema (422)
        # before the service even sees the call.
        empty = await c.post(
            f"/tasks/{tid}/checklist",
            headers=h,
            json={"text": "   "},
        )
        # min_length=1 fires when the field is the empty string. Whitespace
        # only is caught by the service (DomainError 400). Both are valid.
        assert empty.status_code in (400, 422)

        # Add three items; positions are assigned server-side and stay
        # monotonically increasing.
        items: list[dict[str, Any]] = []
        for label in ("bread", "milk", "eggs"):
            r = await c.post(
                f"/tasks/{tid}/checklist",
                headers=h,
                json={"text": label},
            )
            assert r.status_code == 201
            items.append(r.json())
        assert [it["text"] for it in items] == ["bread", "milk", "eggs"]
        positions = [int(it["position"]) for it in items]
        assert positions == sorted(positions) and len(set(positions)) == 3

        # GET /tasks/{id} embeds the checklist in the task payload (same
        # order as GET /tasks/{id}/checklist), so the SPA gets it on
        # first load without a second round-trip.
        task_full = (await c.get(f"/tasks/{tid}", headers=h)).json()
        assert [it["id"] for it in task_full["checklist"]] == [it["id"] for it in items]

        # Tick "milk" done; done_at and done_by are stamped, version
        # bumps.
        milk = items[1]
        r = await c.patch(
            f"/tasks/{tid}/checklist/{milk['id']}",
            headers=h,
            json={"expected_version": milk["version"], "done": True},
        )
        assert r.status_code == 200
        ticked = r.json()
        assert ticked["done"] is True
        assert ticked["done_at"] is not None
        assert ticked["done_by"] is not None
        assert ticked["version"] == milk["version"] + 1

        # Stale version -> 409 ConflictError (mapped to concurrency.stale_version).
        stale = await c.patch(
            f"/tasks/{tid}/checklist/{milk['id']}",
            headers=h,
            json={"expected_version": milk["version"], "done": False},
        )
        assert stale.status_code == 409

        # Un-tick with the fresh version; done_at / done_by go back to NULL.
        r = await c.patch(
            f"/tasks/{tid}/checklist/{milk['id']}",
            headers=h,
            json={"expected_version": ticked["version"], "done": False},
        )
        assert r.status_code == 200
        unticked = r.json()
        assert unticked["done"] is False
        assert unticked["done_at"] is None
        assert unticked["done_by"] is None

        # Rename "eggs" -> "eggs (free range)".
        eggs = items[2]
        r = await c.patch(
            f"/tasks/{tid}/checklist/{eggs['id']}",
            headers=h,
            json={
                "expected_version": eggs["version"],
                "text": "eggs (free range)",
            },
        )
        assert r.status_code == 200
        assert r.json()["text"] == "eggs (free range)"

        # Reorder: move "eggs" to the top. The payload must list every
        # current id; a mismatch is rejected so a stale UI can't silently
        # drop items added in another tab.
        eggs_id = eggs["id"]
        bread_id = items[0]["id"]
        milk_id = items[1]["id"]
        reorder = await c.post(
            f"/tasks/{tid}/checklist:reorder",
            headers=h,
            json={"ids": [eggs_id, bread_id, milk_id]},
        )
        assert reorder.status_code == 200
        ordered = reorder.json()
        assert [it["id"] for it in ordered] == [eggs_id, bread_id, milk_id]

        # Mismatched reorder payload (one id missing) is a domain error.
        bad = await c.post(
            f"/tasks/{tid}/checklist:reorder",
            headers=h,
            json={"ids": [eggs_id, bread_id]},
        )
        assert bad.status_code == 400

        # Mark bread done, then clear_done removes just that one.
        bread = next(it for it in ordered if it["id"] == bread_id)
        await c.patch(
            f"/tasks/{tid}/checklist/{bread_id}",
            headers=h,
            json={"expected_version": bread["version"], "done": True},
        )
        cd = await c.post(f"/tasks/{tid}/checklist:clear_done", headers=h)
        assert cd.status_code == 200
        assert cd.json()["removed"] == 1
        remaining = (await c.get(f"/tasks/{tid}/checklist", headers=h)).json()
        assert {it["id"] for it in remaining} == {eggs_id, milk_id}

        # Delete an item by id; list drops it.
        d = await c.delete(
            f"/tasks/{tid}/checklist/{eggs_id}",
            headers=h,
        )
        assert d.status_code == 204
        remaining2 = (await c.get(f"/tasks/{tid}/checklist", headers=h)).json()
        assert {it["id"] for it in remaining2} == {milk_id}

        # The generic PATCH /tasks/{id} does NOT accept checklist payloads.
        # Trying to overwrite the checklist via the task patch endpoint
        # must be ignored / rejected — i.e. the checklist on the task
        # detail after the patch is the same as before.
        before = (await c.get(f"/tasks/{tid}", headers=h)).json()
        await c.patch(
            f"/tasks/{tid}",
            headers=h,
            json={
                "expected_version": before["version"],
                # extra field that the schema must ignore.
                "checklist": [],
            },
        )
        after = (await c.get(f"/tasks/{tid}", headers=h)).json()
        assert {it["id"] for it in after["checklist"]} == {milk_id}


async def test_list_tasks_include_checklist_param() -> None:
    """GET /tasks?include_checklist=true populates the embedded
    ``checklist`` per task (the SPA needs it for the client-side
    free-text filter to match item text). Without the flag the field
    stays empty so large lists don't pay an extra batch query."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = await _signup(c, "C")
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        task = (await c.post("/tasks", headers=h, json={"title": "Shopping"})).json()
        await c.post(
            f"/tasks/{task['id']}/checklist",
            headers=h,
            json={"text": "pane"},
        )
        # Default: list endpoint leaves checklist empty.
        default = (await c.get("/tasks", headers=h)).json()
        shopping_default = next(t for t in default if t["id"] == task["id"])
        assert shopping_default["checklist"] == []
        # With the flag: items are embedded.
        with_items = (await c.get("/tasks?include_checklist=true", headers=h)).json()
        shopping = next(t for t in with_items if t["id"] == task["id"])
        assert [it["text"] for it in shopping["checklist"]] == ["pane"]


async def test_checklist_isolated_per_task() -> None:
    """Items of task A must not leak into task B's checklist, even
    inside the same workspace. Tenant isolation is enforced by RLS at
    the org level; this checks the task scoping in the list endpoint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = await _signup(c, "B")
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        ta = (await c.post("/tasks", headers=h, json={"title": "A"})).json()
        tb = (await c.post("/tasks", headers=h, json={"title": "B"})).json()
        await c.post(
            f"/tasks/{ta['id']}/checklist",
            headers=h,
            json={"text": "a-only"},
        )
        list_a = (await c.get(f"/tasks/{ta['id']}/checklist", headers=h)).json()
        list_b = (await c.get(f"/tasks/{tb['id']}/checklist", headers=h)).json()
        assert {it["text"] for it in list_a} == {"a-only"}
        assert list_b == []
