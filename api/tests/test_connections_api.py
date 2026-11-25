"""Connections over HTTP (ADR-0077): one route set for every mechanism, and
each kind of item guarded by its own scope.

A person connects a note to a task and sees the connection from both ends.
An assistant that may read notes but not tasks cannot list a task's
connections, and sees a note's connections without the ones to tasks; it is
offered only the connections it may write.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": "CONN",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _assistant(c: AsyncClient, h: dict[str, str], scope: list[str]) -> dict[str, str]:
    created = (
        await c.post(
            "/ai-assistants",
            headers={**h, "X-Workspace-Role": "owner"},
            json={"label": "A", "handle": f"a{uuid.uuid4().hex[:8]}", "scope": scope},
        )
    ).json()
    return {
        "Authorization": f"Bearer {created['raw_secret']}",
        "X-Workspace-Id": h["X-Workspace-Id"],
    }


async def test_connect_from_a_note_and_read_from_both_ends_with_scopes() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _signup(c)
        note = (
            await c.post("/notes", headers=h, json={"kind": "text", "title": "N", "text": "n"})
        ).json()
        task = (await c.post("/tasks", headers=h, json={"title": "T"})).json()

        made = await c.post(
            "/connections/connect",
            headers=h,
            json={
                "this_kind": "note",
                "this_id": note["id"],
                "relation": "result_of",
                "direction": "out",
                "that_kind": "task",
                "that_id": task["id"],
            },
        )
        assert made.status_code == 200, made.text
        from_note = (
            await c.get(
                "/connections", headers=h, params={"this_kind": "note", "this_id": note["id"]}
            )
        ).json()
        assert [(x["relation"], x["direction"], x["other_title"]) for x in from_note["items"]] == [
            ("result_of", "out", "T")
        ]
        from_task = (
            await c.get(
                "/connections", headers=h, params={"this_kind": "task", "this_id": task["id"]}
            )
        ).json()
        assert [(x["relation"], x["direction"], x["other_title"]) for x in from_task["items"]] == [
            ("result_of", "in", "N")
        ]
        # What a task can make includes the work structure; a note cannot be
        # a subtask.
        task_choices = {
            (x["relation"], x["direction"], x["other_kind"]) for x in from_task["choices"]
        }
        assert ("subtask_of", "out", "task") in task_choices
        assert not any(r == "subtask_of" and k == "note" for r, _, k in task_choices)

        notes_only = await _assistant(c, h, ["notes:read", "notes:write"])
        refused = await c.get(
            "/connections", headers=notes_only, params={"this_kind": "task", "this_id": task["id"]}
        )
        assert refused.status_code == 403, refused.text
        seen = (
            await c.get(
                "/connections",
                headers=notes_only,
                params={"this_kind": "note", "this_id": note["id"]},
            )
        ).json()
        assert seen["items"] == []
        assert all(x["other_kind"] == "note" for x in seen["choices"])
        cannot = await c.post(
            "/connections/disconnect",
            headers=notes_only,
            json={
                "this_kind": "note",
                "this_id": note["id"],
                "handle": from_note["items"][0]["handle"],
                "that_kind": "task",
            },
        )
        assert cannot.status_code == 403, cannot.text

        gone = await c.post(
            "/connections/disconnect",
            headers=h,
            json={
                "this_kind": "note",
                "this_id": note["id"],
                "handle": from_note["items"][0]["handle"],
                "that_kind": "task",
            },
        )
        assert gone.status_code == 200, gone.text
        after = (
            await c.get(
                "/connections", headers=h, params={"this_kind": "task", "this_id": task["id"]}
            )
        ).json()
        assert after["items"] == []
