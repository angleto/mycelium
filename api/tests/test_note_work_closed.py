"""A note whose linked work is over (task e584ed61).

``work_closed`` on a note list row says: the note has at least one linked
task and every linked task is closed, i.e. in a terminal workflow state,
archived or in the bin. It is the complement of the garden's "open work"
(``note_inert.open_work_exists``) over the notes that have links. It is
computed on every read and nothing is written on the note, which is why a
task coming back (unarchived) turns it off with no other write.

``notes.list(work_closed=true)`` is the garden's "to decide" filter. It is
applied before paging, it keeps every perimeter the list keeps (a proposal
awaiting review is never listed, another workspace is never seen), and it
lists exactly the rows that carry the flag.
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


async def _owner(c: AsyncClient, name: str = "WORK CLOSED") -> dict[str, str]:
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


async def _note(c: AsyncClient, h: dict[str, str], title: str) -> dict[str, Any]:
    r = await c.post("/notes", headers=h, json={"kind": "text", "title": title, "text": title})
    assert r.status_code == 200, r.text
    return r.json()


async def _task(c: AsyncClient, h: dict[str, str], title: str) -> dict[str, Any]:
    r = await c.post("/tasks", headers=h, json={"title": title, "importance": 3, "urgency": 3})
    assert r.status_code == 200, r.text
    return r.json()


async def _link(c: AsyncClient, h: dict[str, str], note_id: str, task_id: str, kind: str) -> None:
    r = await c.post(
        f"/notes/{note_id}/task-links", headers=h, json={"task_id": task_id, "kind": kind}
    )
    assert r.status_code == 200, r.text


async def _finish(c: AsyncClient, h: dict[str, str], task: dict[str, Any]) -> int:
    """Walk the task to ``done`` through the default workflow; returns the
    version after the last move."""
    states = {s["name"]: s for s in (await c.get(f"/tasks/{task['id']}/states", headers=h)).json()}
    assert states["done"]["is_terminal"] is True
    assert states["in_progress"]["is_terminal"] is False
    version = task["version"]
    for st in ("in_progress", "done"):
        r = await c.post(
            f"/tasks/{task['id']}/state",
            headers=h,
            json={"expected_version": version, "state_id": states[st]["id"]},
        )
        assert r.status_code == 200, r.text
        version = r.json()["version"]
    return int(version)


async def _rows(c: AsyncClient, h: dict[str, str], **params: Any) -> dict[str, dict[str, Any]]:
    r = await c.get("/notes", headers=h, params={"limit": 200, **params})
    assert r.status_code == 200, r.text
    return {n["id"]: n for n in r.json()["items"]}


async def _flags(c: AsyncClient, h: dict[str, str]) -> dict[str, bool]:
    return {nid: row["work_closed"] for nid, row in (await _rows(c, h)).items()}


async def test_the_flag_follows_the_state_of_every_linked_task() -> None:
    """No task, an open task, a done task, one done and one open, a task
    archived while still open, a task in the bin: each against the flag."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        plain = await _note(c, h, "no task at all")
        open_ = await _note(c, h, "its task is open")
        done = await _note(c, h, "its task is done")
        mixed = await _note(c, h, "one done, one open")
        archived = await _note(c, h, "its task was archived while open")
        binned = await _note(c, h, "its task is in the bin")

        t_open = await _task(c, h, "open work")
        await _link(c, h, open_["id"], t_open["id"], "subject")

        t_done = await _task(c, h, "finished work")
        await _finish(c, h, t_done)
        # The motivating case is an artifact link to a done task.
        await _link(c, h, done["id"], t_done["id"], "artifact")

        t_done2 = await _task(c, h, "also finished")
        await _finish(c, h, t_done2)
        t_open2 = await _task(c, h, "still going")
        await _link(c, h, mixed["id"], t_done2["id"], "artifact")
        await _link(c, h, mixed["id"], t_open2["id"], "subject")

        t_arch = await _task(c, h, "shelved")
        await _link(c, h, archived["id"], t_arch["id"], "subject")
        r = await c.post(
            f"/tasks/{t_arch['id']}/archive",
            headers=h,
            json={"expected_version": t_arch["version"]},
        )
        assert r.status_code == 200, r.text

        # A task generated from the note: the link is derived_from, a kind
        # the link endpoint does not write, so every kind is exercised.
        r = await c.post(
            f"/notes/{binned['id']}/derive-task",
            headers=h,
            json={"title": "dropped", "description": None, "estimate_effort_h": None},
        )
        assert r.status_code == 200, r.text
        t_bin = (await c.get(f"/tasks/{r.json()['task_id']}", headers=h)).json()
        r = await c.post(
            f"/tasks/{t_bin['id']}/delete", headers=h, json={"expected_version": t_bin["version"]}
        )
        assert r.status_code == 200, r.text

        flags = await _flags(c, h)
        assert flags[plain["id"]] is False
        assert flags[open_["id"]] is False
        assert flags[done["id"]] is True
        assert flags[mixed["id"]] is False
        assert flags[archived["id"]] is True
        assert flags[binned["id"]] is True


async def test_the_flag_is_computed_and_writes_nothing_on_the_note() -> None:
    """Bringing the task back from the archive turns the flag off with no
    write to the note: same version, and the note's own row never moved."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        n = await _note(c, h, "follows its task")
        t = await _task(c, h, "shelved, then resumed")
        await _link(c, h, n["id"], t["id"], "subject")
        before = (await _rows(c, h))[n["id"]]
        assert before["work_closed"] is False

        r = await c.post(
            f"/tasks/{t['id']}/archive", headers=h, json={"expected_version": t["version"]}
        )
        assert r.status_code == 200, r.text
        closed = (await _rows(c, h))[n["id"]]
        assert closed["work_closed"] is True
        r = await c.post(
            f"/tasks/{t['id']}/unarchive",
            headers=h,
            json={"expected_version": r.json()["version"]},
        )
        assert r.status_code == 200, r.text
        reopened = (await _rows(c, h))[n["id"]]
        assert reopened["work_closed"] is False
        for row in (closed, reopened):
            assert row["version"] == before["version"]
            assert row["updated_at"] == before["updated_at"]
            assert row["maturity"] == before["maturity"]


async def test_the_filter_lists_exactly_the_flagged_notes_before_paging() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        closed_old = await _note(c, h, "closed, older")
        t1 = await _task(c, h, "done long ago")
        await _finish(c, h, t1)
        await _link(c, h, closed_old["id"], t1["id"], "artifact")
        open_ = await _note(c, h, "open, newer")
        t2 = await _task(c, h, "ongoing")
        await _link(c, h, open_["id"], t2["id"], "subject")
        plain = await _note(c, h, "plain, newest")

        flags = await _flags(c, h)
        to_decide = await _rows(c, h, work_closed="true")
        others = await _rows(c, h, work_closed="false")
        assert set(to_decide) == {nid for nid, f in flags.items() if f} == {closed_old["id"]}
        assert set(others) == {nid for nid, f in flags.items() if not f}
        assert set(others) == {open_["id"], plain["id"]}
        assert all(row["work_closed"] for row in to_decide.values())

        # Applied before the page is cut: the closed note is the oldest, and
        # a one-row page still finds it instead of an empty page.
        r = await c.get("/notes", headers=h, params={"work_closed": "true", "limit": 1})
        assert [row["id"] for row in r.json()["items"]] == [closed_old["id"]]
        assert r.json()["next_cursor"] is None
        # And on the touch ordering, which reads the list's base query.
        r = await c.get(
            "/notes",
            headers=h,
            params={"work_closed": "true", "order_by": "touched_at", "order_desc": "true"},
        )
        assert r.status_code == 200, r.text
        assert [row["id"] for row in r.json()["items"]] == [closed_old["id"]]


async def test_the_filter_keeps_the_list_perimeter() -> None:
    """Another workspace's closed notes are not seen, and a proposal still
    awaiting review is not listed even when its work is closed."""
    from sqlalchemy import update

    from mycelium_core.db import tenant_session
    from mycelium_core.models.note import Note

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _owner(c, "WORK CLOSED A")
        b = await _owner(c, "WORK CLOSED B")
        mine = await _note(c, a, "A's closed note")
        proposal = await _note(c, a, "A's closed proposal")
        t = await _task(c, a, "done in A")
        await _finish(c, a, t)
        await _link(c, a, mine["id"], t["id"], "artifact")
        await _link(c, a, proposal["id"], t["id"], "artifact")
        theirs = await _note(c, b, "B's closed note")
        tb = await _task(c, b, "done in B")
        await _finish(c, b, tb)
        await _link(c, b, theirs["id"], tb["id"], "artifact")

        me = (await c.get("/auth/me", headers=a)).json()
        async with tenant_session(a["X-Workspace-Id"], str(me["user_id"])) as s:
            await s.execute(
                update(Note)
                .where(Note.id == uuid.UUID(proposal["id"]))
                .values(review_state="proposed")
            )

        assert set(await _rows(c, a, work_closed="true")) == {mine["id"]}
        assert set(await _rows(c, b, work_closed="true")) == {theirs["id"]}
        assert proposal["id"] not in await _rows(c, a, work_closed="false")
        assert proposal["id"] not in await _rows(c, a, work_closed="true", include_deleted="true")


async def test_the_single_note_read_does_not_claim_the_flag() -> None:
    """Only the list row computes it, so the note read does not carry a
    default that would read as "the work is not closed"."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        n = await _note(c, h, "read alone")
        t = await _task(c, h, "done")
        await _finish(c, h, t)
        await _link(c, h, n["id"], t["id"], "artifact")
        r = await c.get(f"/notes/{n['id']}", headers=h)
        assert r.status_code == 200, r.text
        assert "work_closed" not in r.json()


async def test_the_mcp_tool_is_the_same_operation() -> None:
    from mycelium_core.db import admin_session
    from mycelium_core.services.auth import signup
    from mycelium_mcp import tools_notes

    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP WORK CLOSED",
        )
    token, org = r.token, str(r.org_id)
    assert token is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = {"Authorization": f"Bearer {token}", "X-Workspace-Id": org}
        closed = await _note(c, h, "closed over MCP")
        await _note(c, h, "not closed")
        t = await _task(c, h, "done")
        await _finish(c, h, t)
        await _link(c, h, closed["id"], t["id"], "artifact")

    list_notes = tools_notes.TOOLS["list_notes"]
    page = await list_notes(token=token, org_id=org, work_closed=True)
    assert [row["id"] for row in page["items"]] == [closed["id"]]
    assert page["items"][0]["work_closed"] is True


async def test_a_transplanted_note_is_never_flagged() -> None:
    """Promoting a note links it to the new task (promoted_from); once that
    task is done every linked task is closed, but the note is read-only and
    there is nothing left to decide on it, so it is neither flagged nor
    listed by the filter."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        n = await _note(c, h, "became a task")
        r = await c.post(f"/notes/{n['id']}/promote", headers=h, json={"title": None})
        assert r.status_code == 200, r.text
        task = (await c.get(f"/tasks/{r.json()['task_id']}", headers=h)).json()
        await _finish(c, h, task)
        row = (await _rows(c, h))[n["id"]]
        assert row["promoted_at"] is not None
        assert row["linked_task_count"] == 1
        assert row["work_closed"] is False
        assert n["id"] not in await _rows(c, h, work_closed="true")
        assert n["id"] in await _rows(c, h, work_closed="false")


async def test_a_link_to_another_workspaces_task_is_not_work() -> None:
    """A link row pointing at a done task of another workspace (written past
    the API, with the owner role) is work this workspace cannot see: the
    flag stays off and the filter does not list the note."""
    import os

    import psycopg

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _owner(c, "WORK CLOSED X A")
        b = await _owner(c, "WORK CLOSED X B")
        n = await _note(c, a, "A's note")
        tb = await _task(c, b, "done in B")
        await _finish(c, b, tb)
        owner_dsn = os.environ["MYCELIUM_DATABASE_URL_SYNC"].replace(
            "postgresql+psycopg", "postgresql"
        )
        with psycopg.connect(owner_dsn) as cn:
            cn.execute(
                "INSERT INTO note_task_link (id, org_id, note_id, task_id, kind)"
                " VALUES (%s, %s, %s, %s, 'artifact')",
                (uuid.uuid4(), a["X-Workspace-Id"], n["id"], tb["id"]),
            )
        assert (await _rows(c, a))[n["id"]]["work_closed"] is False
        assert n["id"] not in await _rows(c, a, work_closed="true")
        # The same note linked to a done task of its own workspace is flagged:
        # the link above was refused for the workspace, not for being a link.
        ta = await _task(c, a, "done in A")
        await _finish(c, a, ta)
        await _link(c, a, n["id"], ta["id"], "artifact")
        assert (await _rows(c, a))[n["id"]]["work_closed"] is True
        assert n["id"] in await _rows(c, a, work_closed="true")


async def test_the_flag_states_the_workspace_itself() -> None:
    """The per-row flag and the filter name the workspace in the query, not
    only through row-level security: on an owner connection, which the
    policies do not narrow, another workspace's closed note answers nothing."""
    import os

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from mycelium_core.models.note import Note
    from mycelium_core.services import note_inert

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a = await _owner(c, "WORK CLOSED ORG A")
        b = await _owner(c, "WORK CLOSED ORG B")
        n = await _note(c, a, "A's closed note")
        t = await _task(c, a, "done in A")
        await _finish(c, a, t)
        await _link(c, a, n["id"], t["id"], "artifact")

    owner_url = os.environ["MYCELIUM_DATABASE_URL_SYNC"].replace(
        "postgresql+psycopg", "postgresql+asyncpg"
    )
    engine = create_async_engine(owner_url)
    try:
        async with AsyncSession(engine) as s:
            org_a, org_b, nid = (
                uuid.UUID(a["X-Workspace-Id"]),
                uuid.UUID(b["X-Workspace-Id"]),
                uuid.UUID(n["id"]),
            )
            assert await note_inert.work_closed_note_ids(s, org_id=org_a, note_ids=[nid]) == {nid}
            assert await note_inert.work_closed_note_ids(s, org_id=org_b, note_ids=[nid]) == set()
            listed = set(
                (await s.scalars(select(Note.id).where(note_inert.work_closed(org_b)))).all()
            )
            assert nid not in listed
            listed = set(
                (await s.scalars(select(Note.id).where(note_inert.work_closed(org_a)))).all()
            )
            assert nid in listed
    finally:
        await engine.dispose()
