"""F6 API end-to-end (DB-backed): metered memory write, hybrid search,
GDPR erase, hard (org, project) isolation. Embedder factory overridden
with a deterministic in-memory one (ADR-0012 seam)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.embedder import set_embedder_override


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_f6_api_flow(_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
            )
        ).json()
        b = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "B"},
            )
        ).json()
        h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}

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

        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "the budget review for project alpha",
                "operation_id": "w1",
                "sources": [["email", "m1"]],
            },
        )
        assert w.status_code == 200 and w.json()["tier"] == "hot"
        blob_id = w.json()["id"]

        found = await c.post(
            "/memory/search",
            headers=h,
            json={"project_id": proj, "query": "budget alpha", "operation_id": "q1"},
        )
        assert found.status_code == 200
        assert blob_id in {x["blob"]["id"] for x in found.json()}

        # Hard isolation: a different project sees nothing.
        other = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": str(uuid.uuid4()),
                "query": "budget alpha",
                "operation_id": "q2",
            },
        )
        assert other.json() == []

        erased = await c.post(
            "/memory/erase",
            headers=h,
            json={"source_kind": "email", "source_id": "m1"},
        )
        assert erased.status_code == 200 and erased.json()["deleted"] == 1

        cross = await c.post(
            "/memory/search",
            headers={"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": b["workspace_id"]},
            json={"project_id": proj, "query": "x", "operation_id": "q3"},
        )
        assert cross.status_code == 403
