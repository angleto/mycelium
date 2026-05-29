"""GET /notes/links — workspace-wide listing of note-to-note links
(garden mindmap, ADR-0029 P2).

The endpoint must:
- return every link in the workspace, regardless of which note is
  the anchor (one round-trip, replaces N per-note fetches);
- order links statically (no requirement, but the response is a
  flat list, so any stable iteration is fine);
- not leak links from other workspaces;
- match the literal path before the ``/{note_id}/links`` route
  collision: a UUID in ``note_id`` slot must NOT shadow this.
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
            json={"email": _email(), "password": "pw-strong-123"},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _make_note(c: AsyncClient, h: dict[str, str], title: str) -> str:
    r = await c.post(
        "/notes",
        headers=h,
        json={"kind": "text", "title": title, "text": f"body of {title}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_list_workspace_note_links_returns_every_edge() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        a = await _make_note(c, h, "alpha")
        b = await _make_note(c, h, "beta")
        cc = await _make_note(c, h, "gamma")

        r1 = await c.post(
            f"/notes/{a}/links",
            headers=h,
            json={"parent_note_id": a, "child_note_id": b, "kind": "related"},
        )
        assert r1.status_code == 200, r1.text
        r2 = await c.post(
            f"/notes/{b}/links",
            headers=h,
            json={"parent_note_id": b, "child_note_id": cc, "kind": "hypha_of"},
        )
        assert r2.status_code == 200, r2.text

        r = await c.get("/notes/links", headers=h)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 2
        # ``related`` is undirected (canonicalised parent < child), so
        # match its pair order-agnostically; ``hypha_of`` is directional.
        edges = {(frozenset({x["parent_note_id"], x["child_note_id"]}), x["kind"]) for x in rows}
        assert (frozenset({a, b}), "related") in edges
        assert (frozenset({b, cc}), "hypha_of") in edges


async def test_list_workspace_note_links_isolated_per_workspace() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h1 = await _signup(c)
        a = await _make_note(c, h1, "in-ws1-a")
        b = await _make_note(c, h1, "in-ws1-b")
        await c.post(
            f"/notes/{a}/links",
            headers=h1,
            json={"parent_note_id": a, "child_note_id": b, "kind": "related"},
        )

        h2 = await _signup(c)
        x = await _make_note(c, h2, "in-ws2-x")
        y = await _make_note(c, h2, "in-ws2-y")
        await c.post(
            f"/notes/{x}/links",
            headers=h2,
            json={"parent_note_id": x, "child_note_id": y, "kind": "hypha_of"},
        )

        r1 = await c.get("/notes/links", headers=h1)
        r2 = await c.get("/notes/links", headers=h2)
        assert r1.status_code == 200 and r2.status_code == 200
        rows1 = r1.json()
        rows2 = r2.json()
        assert len(rows1) == 1 and rows1[0]["kind"] == "related"
        assert len(rows2) == 1 and rows2[0]["kind"] == "hypha_of"


async def test_list_workspace_links_does_not_shadow_single_note_route() -> None:
    # The literal path /notes/links must not interfere with /notes/{note_id}/links
    # when {note_id} is a real UUID. Sanity check that the per-note GET
    # still returns the NoteWithLinksOut envelope, not a flat list.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        a = await _make_note(c, h, "anchor")
        r = await c.get(f"/notes/{a}/links", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "note" in body and "outgoing" in body and "incoming" in body
