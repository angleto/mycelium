"""A person's pinned tasks (migration 0027).

What a pin promises, each asserted against the running API: it is the
person's and not the workspace's, it outlives the task being closed or
archived, it changes nothing on the task, the pinned list is newest pin
first, and both directions are idempotent because the control is a toggle
pressed from several lists at once.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


async def _owner(c: AsyncClient, name: str = "PINS") -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": name,
            },
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _task(c: AsyncClient, h: dict[str, str], title: str) -> dict:
    r = await c.post("/tasks", headers=h, json={"title": title})
    assert r.status_code == 200, r.text
    return r.json()


async def _pinned(c: AsyncClient, h: dict[str, str]) -> list[str]:
    r = await c.get("/tasks", headers=h, params={"pinned": "true"})
    assert r.status_code == 200, r.text
    return [t["id"] for t in r.json()]


async def test_pin_lists_newest_first_and_unpin_removes() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        a = await _task(c, h, "first pinned")
        b = await _task(c, h, "second pinned")
        await _task(c, h, "never pinned")
        assert await _pinned(c, h) == []

        # Pinned in the reverse of the order they were created in, so the
        # list's order can only come from the pins and not from the tasks.
        r = await c.post("/tasks/pin", headers=h, json={"task_id": b["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["pinned"] is True and r.json()["pinned_at"]
        await c.post("/tasks/pin", headers=h, json={"task_id": a["id"]})
        assert await _pinned(c, h) == [a["id"], b["id"]]

        r = await c.post("/tasks/unpin", headers=h, json={"task_id": a["id"]})
        assert r.status_code == 200 and r.json()["pinned"] is False
        assert await _pinned(c, h) == [b["id"]]


async def test_both_directions_are_idempotent_and_repinning_keeps_the_order() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        a = await _task(c, h, "a")
        b = await _task(c, h, "b")
        first = (await c.post("/tasks/pin", headers=h, json={"task_id": a["id"]})).json()
        await c.post("/tasks/pin", headers=h, json={"task_id": b["id"]})
        again = await c.post("/tasks/pin", headers=h, json={"task_id": a["id"]})
        assert again.status_code == 200
        # The first pin's time stands, so pressing it again does not move
        # the task to the top: the order is the order things were pinned in.
        assert again.json()["pinned_at"] == first["pinned_at"]
        assert await _pinned(c, h) == [b["id"], a["id"]]

        for _ in range(2):
            r = await c.post("/tasks/unpin", headers=h, json={"task_id": a["id"]})
            assert r.status_code == 200
        assert await _pinned(c, h) == [b["id"]]


async def test_a_closed_or_archived_task_stays_pinned() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        done = await _task(c, h, "closed but at hand")
        archived = await _task(c, h, "archived but at hand")
        for t in (done, archived):
            await c.post("/tasks/pin", headers=h, json={"task_id": t["id"]})

        states = {
            s["name"]: s["id"]
            for s in (await c.get(f"/tasks/{done['id']}/states", headers=h)).json()
        }
        version = done["version"]
        for name in ("in_progress", "done"):
            r = await c.post(
                f"/tasks/{done['id']}/state",
                headers=h,
                json={"expected_version": version, "state_id": states[name]},
            )
            assert r.status_code == 200, r.text
            version = r.json()["version"]
        r = await c.post(
            f"/tasks/{archived['id']}/archive",
            headers=h,
            json={"expected_version": archived["version"]},
        )
        assert r.status_code == 200, r.text

        assert set(await _pinned(c, h)) == {done["id"], archived["id"]}
        # And it can be unpinned from there: that is the control the list
        # of pins carries.
        await c.post("/tasks/unpin", headers=h, json={"task_id": done["id"]})
        assert await _pinned(c, h) == [archived["id"]]


async def test_pinning_changes_nothing_on_the_task() -> None:
    """A pin is not an edit: no version bump (an open editor would see a
    conflict) and no ``updated_at`` bump (the Recent list would reorder)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        t = await _task(c, h, "untouched")
        before = (await c.get(f"/tasks/{t['id']}", headers=h)).json()
        await c.post("/tasks/pin", headers=h, json={"task_id": t["id"]})
        await c.post("/tasks/unpin", headers=h, json={"task_id": t["id"]})
        after = (await c.get(f"/tasks/{t['id']}", headers=h)).json()
        assert after["version"] == before["version"]
        assert after["updated_at"] == before["updated_at"]


async def test_a_task_in_the_bin_cannot_be_pinned_and_drops_out_of_the_list() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        t = await _task(c, h, "binned")
        await c.post("/tasks/pin", headers=h, json={"task_id": t["id"]})
        r = await c.post(
            f"/tasks/{t['id']}/delete", headers=h, json={"expected_version": t["version"]}
        )
        assert r.status_code == 200, r.text
        assert await _pinned(c, h) == []
        r = await c.post("/tasks/pin", headers=h, json={"task_id": t["id"]})
        assert r.status_code == 404, r.text
        # A pin can always be cleared, even on a task in the bin.
        r = await c.post("/tasks/unpin", headers=h, json={"task_id": t["id"]})
        assert r.status_code == 200, r.text


async def test_pins_are_personal() -> None:
    """Another member of the same workspace sees none of my pins."""
    from mycelium_core.db import admin_session, tenant_session
    from mycelium_core.models.membership import Membership, Role
    from mycelium_core.services.auth import login, signup

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        owner_email = f"{uuid.uuid4().hex[:10]}@example.test"
        member_email = f"{uuid.uuid4().hex[:10]}@example.test"
        async with admin_session() as s:
            owner = await signup(s, email=owner_email, password="pw-strong-123", org_name="A")
            member = await signup(s, email=member_email, password="pw-strong-123", org_name="B")
        async with tenant_session(str(owner.org_id), str(owner.user_id)) as s:
            s.add(Membership(org_id=owner.org_id, user_id=member.user_id, role=Role.member))
            await s.flush()
        async with admin_session() as s:
            mine = await login(s, email=owner_email, password="pw-strong-123")
            theirs = await login(s, email=member_email, password="pw-strong-123")
        ws = str(owner.org_id)
        h_me = {"Authorization": f"Bearer {mine.access_token}", "X-Workspace-Id": ws}
        h_them = {"Authorization": f"Bearer {theirs.access_token}", "X-Workspace-Id": ws}

        t = await _task(c, h_me, "shared task, my pin")
        await c.post("/tasks/pin", headers=h_me, json={"task_id": t["id"]})
        assert await _pinned(c, h_me) == [t["id"]]
        assert await _pinned(c, h_them) == []


async def test_a_task_of_another_workspace_cannot_be_pinned() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c, "MINE")
        other = await _owner(c, "OTHER")
        foreign = await _task(c, other, "not yours")
        r = await c.post("/tasks/pin", headers=h, json={"task_id": foreign["id"]})
        assert r.status_code == 404, r.text
        assert await _pinned(c, other) == []


async def test_the_mcp_tools_are_the_same_operation() -> None:
    from mycelium_core.db import admin_session
    from mycelium_core.services.auth import signup
    from mycelium_mcp import tools_tasks
    from mycelium_mcp.server import list_tasks

    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP PINS",
        )
    token, org = r.token, str(r.org_id)
    assert token is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = {"Authorization": f"Bearer {token}", "X-Workspace-Id": org}
        t = await _task(c, h, "pinned over MCP")

    pinned = await tools_tasks.TOOLS["pin_task"](token=token, org_id=org, task_id=t["id"])
    assert pinned["pinned"] is True
    page = await list_tasks(token=token, org_id=org, pinned=True)
    assert [row["id"] for row in page["items"]] == [t["id"]]
    await tools_tasks.TOOLS["unpin_task"](token=token, org_id=org, task_id=t["id"])
    page = await list_tasks(token=token, org_id=org, pinned=True)
    assert page["items"] == []
