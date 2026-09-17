"""The possession routes a board reads, over HTTP.

They had no test at this level, and that is how both collection routes
shipped unreachable: declared below ``GET /tasks/{task_id}``, Starlette
handed them to it and the caller was told that "leases" is not a uuid.
``test_route_shadowing`` refuses that shape structurally; this file is
the other half, because a route can be reachable and still answer the
wrong thing.

What is pinned here is what the interface actually asks:

* who holds what across the workspace, with the holder's NAME resolved
  server-side -- a uuid is not an answer to "who has this";
* who passed each task on, one row per task and only for releases that
  were a handoff, because "who finished this" and "whose session died"
  answer a different question;
* the working sessions that are open.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _states(c: AsyncClient, h: dict[str, str], task_id: str) -> dict[str, str]:
    rows = (await c.get(f"/tasks/{task_id}/states", headers=h)).json()
    return {r["name"]: r["id"] for r in rows}


async def test_the_board_reads_who_holds_what_and_who_passed_it_on() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        held = (await c.post("/tasks", headers=h, json={"title": "held"})).json()
        passed = (await c.post("/tasks", headers=h, json={"title": "passed on"})).json()
        states = await _states(c, h, held["id"])

        worker = (await c.post("/tasks/workers", headers=h, json={"label": "verify-3"})).json()
        author = (await c.post("/tasks/workers", headers=h, json={"label": "w7"})).json()

        # One task held right now.
        r = await c.post(f"/tasks/{held['id']}/leases", headers=h, json={"worker_id": worker["id"]})
        assert r.status_code == 200, r.text

        # And one handed on: taken, then moved, which releases it in the
        # same transaction with reason ``handoff``.
        r = await c.post(
            f"/tasks/{passed['id']}/leases", headers=h, json={"worker_id": author["id"]}
        )
        assert r.status_code == 200, r.text
        r = await c.post(
            f"/tasks/{passed['id']}/state",
            headers=h,
            json={
                "state_id": states["in_progress"],
                "expected_version": passed["version"],
                "worker_id": author["id"],
            },
        )
        assert r.status_code == 200, r.text

        live = await c.get("/tasks/leases", headers=h)
        assert live.status_code == 200, live.text
        rows = live.json()
        assert [x["task_id"] for x in rows] == [held["id"]]
        # The NAME, resolved once on the server: neither a person nor an
        # agent can resolve a uuid, and asking per row is a round trip
        # for a column.
        assert rows[0]["holder_label"] == "verify-3"

        handed = await c.get("/tasks/leases/last-handoff", headers=h)
        assert handed.status_code == 200, handed.text
        rows = handed.json()
        assert [x["task_id"] for x in rows] == [passed["id"]]
        assert rows[0]["holder_label"] == "w7"
        assert rows[0]["release_reason"] == "handoff"

        # A session that is open is visible as such, which is the other
        # question: a worker holding nothing is running and idle, and
        # nothing else in the app could say so.
        sessions = await c.get("/tasks/workers", headers=h)
        assert sessions.status_code == 200, sessions.text
        assert {w["label"] for w in sessions.json()} == {"verify-3", "w7"}


async def test_finishing_a_task_is_not_passing_it_on() -> None:
    """``done`` releases the lease too, and answering "who passed this to
    me" with "nobody, it is finished" would put a name on every closed
    card at the station where the rule about not checking your own work
    is read."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "finished"})).json()
        states = await _states(c, h, task["id"])
        worker = (await c.post("/tasks/workers", headers=h, json={"label": "w9"})).json()

        # Into the working station first, with nobody holding it: the
        # workflow has no todo -> done edge, and taking the lease only
        # once the task is where the work happens is also what a session
        # actually does.
        moved = await c.post(
            f"/tasks/{task['id']}/state",
            headers=h,
            json={"state_id": states["in_progress"], "expected_version": task["version"]},
        )
        assert moved.status_code == 200, moved.text

        await c.post(f"/tasks/{task['id']}/leases", headers=h, json={"worker_id": worker["id"]})
        r = await c.post(
            f"/tasks/{task['id']}/state",
            headers=h,
            json={
                "state_id": states["done"],
                "expected_version": moved.json()["version"],
                "worker_id": worker["id"],
            },
        )
        assert r.status_code == 200, r.text

        assert (await c.get("/tasks/leases", headers=h)).json() == []
        assert (await c.get("/tasks/leases/last-handoff", headers=h)).json() == []


async def test_the_holder_name_is_resolved_for_a_whole_page_at_once() -> None:
    """The projection resolves names for the page in a bounded number of
    queries, not one per row: the board asks for every live possession at
    once, and a per-row lookup turns a cheap read into one round trip per
    session. Asserted as behaviour -- several holders of BOTH kinds in
    one page, each named correctly -- because the count itself is not
    visible from here.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        expected: dict[str, str] = {}
        for i in range(4):
            task = (await c.post("/tasks", headers=h, json={"title": f"t{i}"})).json()
            w = (await c.post("/tasks/workers", headers=h, json={"label": f"w{i}"})).json()
            await c.post(f"/tasks/{task['id']}/leases", headers=h, json={"worker_id": w["id"]})
            expected[task["id"]] = f"w{i}"
        # And one held with no worker at all, which resolves to the user
        # instead and is the branch a page-wide lookup can silently drop.
        bare = (await c.post("/tasks", headers=h, json={"title": "bare"})).json()
        r = await c.post(f"/tasks/{bare['id']}/leases", headers=h, json={})
        assert r.status_code == 422, "worker_id is required on this route"

        rows = (await c.get("/tasks/leases", headers=h)).json()
        assert {x["task_id"]: x["holder_label"] for x in rows} == expected
