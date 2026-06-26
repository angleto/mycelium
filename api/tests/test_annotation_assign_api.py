"""Annotation assignment endpoints (task 861b360b, 1f161485 #1):
POST /annotations/{id}/assign + GET /annotations/assigned.

Thin-adapter test pinning the HTTP contract: assign by identity id, the
"assigned to me" inbox (default = caller), clear, the unknown-handle 404, and
that the static ``/assigned`` path is matched before ``/{annotation_id}``.
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


async def _comment(c: AsyncClient, h: dict[str, str]) -> dict:
    note = (await c.post("/notes", headers=h, json={"kind": "text", "text": "body"})).json()
    part0 = (await c.get(f"/notes/{note['id']}", headers=h)).json()["parts"][0]["id"]
    r = await c.post(
        "/annotations/comment",
        headers=h,
        json={"doc_kind": "note_part", "doc_id": part0, "body": "look here"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_assign_inbox_and_clear_round_trip() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        a = await _comment(c, h)
        me = a["author_identity_id"]

        # Assign to myself (by identity id).
        r = await c.post(
            f"/annotations/{a['id']}/assign",
            headers=h,
            json={"expected_version": a["version"], "assignee_identity_id": me},
        )
        assert r.status_code == 200, r.text
        v2 = r.json()["version"]

        # The annotation now carries the assignee.
        got = (await c.get(f"/annotations/{a['id']}", headers=h)).json()
        assert got["assigned_to_identity_id"] == me

        # The "assigned to me" inbox (default = caller) lists it.
        inbox = (await c.get("/annotations/assigned", headers=h)).json()
        assert a["id"] in {x["id"] for x in inbox}

        # Clear it -> gone from the inbox.
        r = await c.post(
            f"/annotations/{a['id']}/assign",
            headers=h,
            json={"expected_version": v2, "clear": True},
        )
        assert r.status_code == 200, r.text
        inbox = (await c.get("/annotations/assigned", headers=h)).json()
        assert a["id"] not in {x["id"] for x in inbox}


async def test_assign_unknown_handle_404() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        a = await _comment(c, h)
        r = await c.post(
            f"/annotations/{a['id']}/assign",
            headers=h,
            json={"expected_version": a["version"], "assignee_handle": "no-such-handle"},
        )
        assert r.status_code == 404, r.text


async def test_assigned_route_does_not_collide_with_id_route() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # /annotations/assigned resolves to the inbox (a list), not the
        # /{annotation_id} detail route (which would 404/422 on "assigned").
        r = await c.get("/annotations/assigned", headers=h)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)
