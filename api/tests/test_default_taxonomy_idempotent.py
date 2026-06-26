"""Regression: the default "Personal" client / "General" project are
idempotent on the natural key uq_tags(org_id, kind, name), NOT on a
denormalized organizations.settings pointer.

Previously, in a brand-new workspace, `create_task` (→ ensure_default_
project → ensure_default_client) followed by a plain `POST /notes`
(→ ensure_default_client again) raised `tag.duplicate`: the settings
pointer is only written when the Organization row is readable, and it
is RLS-hidden in the request session (org is None), so it was never
persisted and the second call re-inserted "Personal" and collided.
The fix makes ensure_default_* get-or-create on the natural key, so
any call order is safe and there is always exactly one default.
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


async def _personal_client_tags(c: AsyncClient, h: dict[str, str]) -> list[dict]:
    tags = (await c.get("/tags", headers=h, params={"kind": "client"})).json()
    return [t for t in tags if t["name"] == "Personal"]


async def test_task_then_plain_note_no_duplicate_default_client() -> None:
    """The exact order that used to raise tag.duplicate."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)

        t = await c.post("/tasks", headers=h, json={"title": "T1"})
        assert t.status_code == 200, t.text

        n = await c.post("/notes", headers=h, json={"title": "N1", "text": "x", "kind": "text"})
        assert n.status_code == 200, n.text  # used to be 400 tag.duplicate

        note = n.json()
        client_tags = [tg for tg in note["tags"] if tg["kind"] == "client"]
        assert [tg["name"] for tg in client_tags] == ["Personal"]

        personals = await _personal_client_tags(c, h)
        assert len(personals) == 1, personals
        # The note's client is THE single default client.
        assert client_tags[0]["id"] == personals[0]["id"]


async def test_note_then_task_still_single_default_client() -> None:
    """The reverse order (the historical workaround) must still hold."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)

        n = await c.post("/notes", headers=h, json={"title": "N", "text": "x", "kind": "text"})
        assert n.status_code == 200, n.text
        t = await c.post("/tasks", headers=h, json={"title": "T"})
        assert t.status_code == 200, t.text

        assert len(await _personal_client_tags(c, h)) == 1


async def test_repeated_task_creation_single_default_project() -> None:
    """create_task is idempotent on the default "General" project too:
    many tasks, still one default project and one default client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)

        for i in range(4):
            r = await c.post("/tasks", headers=h, json={"title": f"T{i}"})
            assert r.status_code == 200, r.text

        assert len(await _personal_client_tags(c, h)) == 1
        projects = (await c.get("/tags", headers=h, params={"kind": "project"})).json()
        generals = [p for p in projects if p["name"] == "General"]
        assert len(generals) == 1, generals
