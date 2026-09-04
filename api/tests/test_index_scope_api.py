"""``index_scope`` over HTTP: the pass-through and the read-back (A1).

The core tests own the indexer behaviour; what this file pins is the
REST wiring, which is where a typed signature stops helping. A field
missing from ``routers/notes._list_out`` falls back to the schema
default instead of failing to type-check, so the list projection would
report a scoped-out note as indexed and nothing would notice.

Also pins the one client error the two PATCH bodies can produce: both
type the field as optional, so a stated ``null`` is well-formed and can
only end as a NOT NULL violation. It answers 422 with the field named,
not 500.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.embedder import set_embedder_override


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "IS"},
        )
    ).json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def test_task_index_scope_round_trips_over_rest(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        created = (
            await c.post(
                "/tasks", headers=h, json={"title": "rest scope task", "index_scope": "none"}
            )
        ).json()
        assert created["index_scope"] == "none"
        tid = created["id"]
        assert (await c.get(f"/tasks/{tid}", headers=h)).json()["index_scope"] == "none"
        r = await c.patch(
            f"/tasks/{tid}",
            headers=h,
            json={"expected_version": created["version"], "index_scope": "org"},
        )
        assert r.status_code == 200, r.text
        assert (await c.get(f"/tasks/{tid}", headers=h)).json()["index_scope"] == "org"


async def test_note_index_scope_round_trips_over_rest(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        created = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "text": "rest scope note", "index_scope": "none"},
            )
        ).json()
        assert created["index_scope"] == "none"
        nid = created["id"]
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["index_scope"] == "none"
        # The list projection is built by a different function than the
        # single-note one, and its schema default would mask a gap.
        rows = (await c.get("/notes", headers=h)).json()
        assert [n["index_scope"] for n in rows if n["id"] == nid] == ["none"]
        r = await c.patch(
            f"/notes/{nid}",
            headers=h,
            json={"expected_version": created["version"], "index_scope": "org"},
        )
        assert r.status_code == 200, r.text
        assert (await c.get(f"/notes/{nid}", headers=h)).json()["index_scope"] == "org"


async def test_a_stated_null_is_a_client_error_on_both_bodies(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "null scope task"})).json()
        r = await c.patch(
            f"/tasks/{task['id']}",
            headers=h,
            json={"expected_version": task["version"], "index_scope": None},
        )
        assert r.status_code == 422, r.text
        note = (
            await c.post("/notes", headers=h, json={"kind": "text", "text": "null scope note"})
        ).json()
        r = await c.patch(
            f"/notes/{note['id']}",
            headers=h,
            json={"expected_version": note["version"], "index_scope": None},
        )
        assert r.status_code == 422, r.text
