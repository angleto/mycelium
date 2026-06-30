"""GET /garden/clusters — Leiden community detection + modularity
(task 8c0a8f08, ADR-0031 v2).

Two tightly-linked triangles joined by a single weak bridge must split
into two communities with positive modularity. A separate test forces
the optional ``clustering`` extra to be unavailable and asserts the
endpoint degrades to an empty map + null modularity rather than 500.
"""

from __future__ import annotations

import sys
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient, ws: str = "C") -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": ws},
        )
    ).json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
    }


async def _make_note(c: AsyncClient, h: dict[str, str], title: str) -> str:
    r = await c.post("/notes", headers=h, json={"kind": "text", "title": title, "text": title})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _link(c: AsyncClient, h: dict[str, str], parent: str, child: str, kind: str) -> None:
    r = await c.post(
        f"/notes/{parent}/links",
        headers=h,
        json={"parent_note_id": parent, "child_note_id": child, "kind": kind},
    )
    assert r.status_code == 200, r.text


async def _triangle(c: AsyncClient, h: dict[str, str], prefix: str) -> list[str]:
    a = await _make_note(c, h, f"{prefix}1")
    b = await _make_note(c, h, f"{prefix}2")
    d = await _make_note(c, h, f"{prefix}3")
    await _link(c, h, a, b, "hypha_of")
    await _link(c, h, b, d, "hypha_of")
    await _link(c, h, d, a, "hypha_of")
    return [a, b, d]


async def test_clusters_split_two_triangles() -> None:
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        a1, a2, a3 = await _triangle(c, h, "A")
        b1, b2, b3 = await _triangle(c, h, "B")
        # One weak cross-cluster bridge: not enough to merge them.
        await _link(c, h, a1, b1, "related")

        r = await c.get("/garden/clusters", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        cl = body["clusters"]
        # Each triangle is internally homogeneous...
        assert cl[a1] == cl[a2] == cl[a3]
        assert cl[b1] == cl[b2] == cl[b3]
        # ...and the two triangles are distinct communities.
        assert cl[a1] != cl[b1]
        assert body["count"] >= 2
        assert body["modularity"] is not None and body["modularity"] > 0.0


async def test_clusters_degrade_when_extra_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the in-function ``import igraph`` to raise ImportError even
    # if the extra is installed (sys.modules[name]=None makes import
    # raise), so the graceful-degrade branch is exercised deterministically.
    monkeypatch.setitem(sys.modules, "igraph", None)
    monkeypatch.setitem(sys.modules, "leidenalg", None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _make_note(c, h, "solo")
        r = await c.get("/garden/clusters", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["clusters"] == {}
        assert body["modularity"] is None
        assert body["count"] == 0
