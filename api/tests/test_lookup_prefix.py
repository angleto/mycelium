"""``GET /lookup/{prefix}`` — UUID-prefix resolver.

Smoke + edge coverage:

* an 8-char prefix matches the task it belongs to;
* a note prefix is matched under ``kind=note``;
* archived/deleted are hidden unless the caller opts in;
* a non-hex string is rejected with 400;
* a prefix that doesn't match anything returns an empty list (not 404).
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
    r = await c.post(
        "/auth/signup",
        json={
            "email": _email(),
            "password": "pw-strong-123",
            "workspace_name": "Lookup",
        },
    )
    a = r.json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _grant_and_rate(c: AsyncClient, h: dict[str, str]) -> None:
    await c.post("/billing/grant", headers=h, json={"amount": "100"})
    await c.post(
        "/billing/rate-cards",
        headers=h,
        json={
            "model_id": FakeEmbedder.model_id,
            "provider": "local",
            "credits_per_input": "0.001",
        },
    )


async def test_task_prefix_resolves(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Roadmap fase 1 stabilizzazione"},
            )
        ).json()
        tid = task["id"]
        prefix = tid[:8]

        r = await c.get(f"/lookup/{prefix}", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["prefix"] == prefix
        match = next(m for m in body["matches"] if m["id"] == tid)
        assert match["kind"] == "task"
        assert match["title"] == "Roadmap fase 1 stabilizzazione"
        assert match["route_url"] == f"/tasks/{tid}"
        assert match["is_archived"] is False
        assert match["is_deleted"] is False
        assert match["state_name"] == "todo"


async def test_note_prefix_resolves(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        note = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "Visione foresta"},
            )
        ).json()
        nid = note["id"]
        prefix = nid[:8]

        r = await c.get(f"/lookup/{prefix}", headers=h, params={"kinds": "note"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert any(
            m["kind"] == "note" and m["id"] == nid and m["route_url"] == f"/notes/{nid}"
            for m in body["matches"]
        ), body


async def test_archived_task_hidden_by_default(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (await c.post("/tasks", headers=h, json={"title": "Archive me"})).json()
        tid = task["id"]
        await c.post(
            f"/tasks/{tid}/archive",
            headers=h,
            json={"expected_version": task["version"]},
        )

        prefix = tid[:8]
        default = (await c.get(f"/lookup/{prefix}", headers=h)).json()
        assert not any(m["id"] == tid for m in default["matches"])

        opted = (
            await c.get(
                f"/lookup/{prefix}",
                headers=h,
                params={"include_archived": "true"},
            )
        ).json()
        assert any(m["id"] == tid and m["is_archived"] for m in opted["matches"])


async def test_archived_note_hidden_by_default_and_reported_when_opted_in(
    _fake_embedder: None,
) -> None:
    """The note twin of the test above (task d12f6217). Both legs were
    broken on this branch: ``include_archived`` was never applied to
    notes, and every note match claimed ``is_archived=False`` because the
    column was not even selected -- so an archived note reached the picker
    labelled as live."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        note = (
            await c.post("/notes", headers=h, json={"kind": "text", "title": "Shelf me"})
        ).json()
        nid = note["id"]
        r = await c.post(
            f"/notes/{nid}/archive",
            headers=h,
            json={"expected_version": note["version"]},
        )
        assert r.status_code == 200, r.text

        prefix = nid[:8]
        default = (await c.get(f"/lookup/{prefix}", headers=h, params={"kinds": "note"})).json()
        assert not any(m["id"] == nid for m in default["matches"]), default

        opted = (
            await c.get(
                f"/lookup/{prefix}",
                headers=h,
                params={"kinds": "note", "include_archived": "true"},
            )
        ).json()
        assert any(m["id"] == nid and m["is_archived"] for m in opted["matches"]), opted


async def test_prefix_validation(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # Too short (< 4).
        r = await c.get("/lookup/abc", headers=h)
        assert r.status_code == 400
        # Non-hex content.
        r = await c.get("/lookup/zzzz", headers=h)
        assert r.status_code == 400


async def test_unknown_prefix_returns_empty(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # Valid hex but nothing in the workspace starts with it.
        r = await c.get("/lookup/ffffffff", headers=h)
        assert r.status_code == 200
        assert r.json()["matches"] == []


async def test_cross_workspace_isolation(_fake_embedder: None) -> None:
    """A prefix that matches in workspace A must not surface in B."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        ha = await _signup(c)
        await _grant_and_rate(c, ha)
        ta = (await c.post("/tasks", headers=ha, json={"title": "Workspace A task"})).json()
        tid = ta["id"]

        hb = await _signup(c)
        prefix = tid[:8]
        body = (await c.get(f"/lookup/{prefix}", headers=hb)).json()
        assert all(m["id"] != tid for m in body["matches"]), body
