"""@note support: notes list endpoint + Apple-Notes auto-title (first
line becomes the title when none is given)."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_notes_list_and_auto_title() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}

        # No title -> first non-empty line becomes the title.
        n1 = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "text": "Refactor scheduler\nsplit CPM core"},
            )
        ).json()
        assert n1["title"] == "Refactor scheduler"

        # Explicit title is kept.
        n2 = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "Keep me", "text": "body line"},
            )
        ).json()
        assert n2["title"] == "Keep me"

        # List endpoint returns them (newest first).
        lst = (await c.get("/notes", headers=h)).json()
        ids = [x["id"] for x in lst]
        assert n1["id"] in ids
        assert n2["id"] in ids
        by_id = {x["id"]: x for x in lst}
        assert by_id[n1["id"]]["title"] == "Refactor scheduler"
