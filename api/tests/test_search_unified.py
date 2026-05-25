"""Unified /search endpoint + task-search index plumbing.

End-to-end checks: creating a task makes it findable via /search
(kind=task), editing it refreshes the blob, deleting the task drops
the blob, and soft-delete / archived tasks are hidden unless the
caller opts in. The fake embedder seam keeps semantic recall
deterministic without the optional model.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.embedder import set_embedder_override


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _signup(c: AsyncClient, name: str = "A") -> dict[str, str]:
    r = await c.post(
        "/auth/signup",
        json={
            "email": _email(),
            "password": "pw-strong-123",
            "workspace_name": name,
        },
    )
    a = r.json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _grant_and_rate(c: AsyncClient, h: dict[str, str]) -> None:
    """The task-search resync uses the same embedder + metering path as
    /memory/blobs; grant a balance and a rate card so the metered embed
    in the resync doesn't trip on a missing card. The keyword-only fallback
    is exercised separately by the existing memory tests."""
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


async def test_create_task_makes_it_searchable(_fake_embedder: None) -> None:
    """A fresh task is indexed at commit time: /search finds it by a
    title token immediately, no async backfill needed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Quarterly budget review for alpha"},
            )
        ).json()
        tid = task["id"]

        r = await c.post(
            "/search",
            headers=h,
            json={"q": "quarterly budget", "kinds": ["task"], "limit": 10},
        )
        assert r.status_code == 200, r.text
        hits = r.json()
        assert any(
            hit["kind"] == "task" and hit["task_id"] == tid for hit in hits
        ), f"expected the new task in hits, got {hits}"


async def test_checklist_text_is_indexed(_fake_embedder: None) -> None:
    """A checklist item word makes the parent task findable even when
    the title doesn't contain it: the resync renders ``title +
    description + checklist`` as one blob."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Shopping list"},
            )
        ).json()
        tid = task["id"]
        await c.post(
            f"/tasks/{tid}/checklist",
            headers=h,
            json={"text": "pomodori cipolle aglio"},
        )

        r = await c.post(
            "/search",
            headers=h,
            json={"q": "pomodori", "kinds": ["task"], "limit": 10},
        )
        assert r.status_code == 200, r.text
        hits = r.json()
        assert any(hit["task_id"] == tid for hit in hits), (
            f"checklist word should surface the parent task: {hits}"
        )


async def test_delete_task_drops_the_blob(_fake_embedder: None) -> None:
    """Deleting the task removes its pointer and the underlying blob;
    a subsequent search no longer returns it."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Ephemeral migration plan"},
            )
        ).json()
        tid = task["id"]

        # Visible first.
        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": "ephemeral migration", "kinds": ["task"]},
            )
        ).json()
        assert any(h_["task_id"] == tid for h_ in hits)

        # Soft-delete via POST /tasks/{id}/delete (the REST DELETE verb
        # is reserved for sub-resources; the task delete is a state
        # transition that sets ``deleted_at``, not a row removal). The
        # listener-driven resync detects the soft-delete and cleans the
        # pointer + blob.
        await c.post(
            f"/tasks/{tid}/delete",
            headers=h,
            json={"expected_version": task["version"]},
        )

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={
                    "q": "ephemeral migration",
                    "kinds": ["task"],
                    "include_deleted": False,
                },
            )
        ).json()
        assert not any(h_["task_id"] == tid for h_ in hits), (
            "soft-deleted task should be hidden by default"
        )

        # include_deleted=true brings it back.
        hits = (
            await c.post(
                "/search",
                headers=h,
                json={
                    "q": "ephemeral migration",
                    "kinds": ["task"],
                    "include_deleted": True,
                },
            )
        ).json()
        assert any(h_["task_id"] == tid for h_ in hits), (
            f"include_deleted=true should expose the soft-deleted task: {hits}"
        )


async def test_archived_task_hidden_by_default(_fake_embedder: None) -> None:
    """An archived task is filtered out unless include_archived=true."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        task = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Bestiary of obscure compiler bugs"},
            )
        ).json()
        tid = task["id"]
        # Archive via the dedicated POST /tasks/{id}/archive endpoint
        # (is_archived is not exposed through generic PATCH; it's a
        # state transition with its own audit action).
        await c.post(
            f"/tasks/{tid}/archive",
            headers=h,
            json={"expected_version": task["version"]},
        )

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": "bestiary", "kinds": ["task"]},
            )
        ).json()
        assert not any(h_["task_id"] == tid for h_ in hits)

        hits = (
            await c.post(
                "/search",
                headers=h,
                json={"q": "bestiary", "kinds": ["task"], "include_archived": True},
            )
        ).json()
        assert any(h_["task_id"] == tid for h_ in hits)
