"""Live ASGI coverage for the provider-selection routers (tasks d2c60a83,
5276207e): /llm-provider and /embedder-provider. Exercises the router +
auth + tenant RLS + Pydantic serialization that the service-layer unit
tests skip. Offline: our_key (no BYOK probe) and an unconfigured key make
the roster endpoint return the curated list with no network call.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    tok = (
        await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})
    ).json()["token"]
    ws = (await c.get("/workspaces", headers={"Authorization": f"Bearer {tok}"})).json()
    # The effective workspace role is the asserted X-Workspace-Role clamped
    # to actual membership (the SPA client mirrors this). A fresh signup owns
    # its personal org, so asserting its role unlocks the admin-gated writes.
    return {
        "Authorization": f"Bearer {tok}",
        "x-workspace-id": ws[0]["id"],
        "X-Workspace-Role": ws[0]["role"],
    }


async def test_llm_provider_endpoints_roundtrip() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _signup(c)
        # Fresh org defaults to local, no key.
        got = (await c.get("/llm-provider", headers=h)).json()
        assert got["provider"] == "local" and got["has_key"] is False

        # Select Scaleway on the platform key (our_key: no api_key -> no probe).
        put = await c.put(
            "/llm-provider",
            headers=h,
            json={"provider": "scaleway", "model": "gpt-oss-120b"},
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["provider"] == "scaleway"
        assert body["model"] == "gpt-oss-120b"
        assert body["has_key"] is False
        # The stored key is never echoed (the schema has no key field).
        assert "api_key" not in put.text and "ciphertext" not in put.text

        # Persisted across a fresh GET.
        again = (await c.get("/llm-provider", headers=h)).json()
        assert again["provider"] == "scaleway" and again["model"] == "gpt-oss-120b"

        # Curated roster (no key configured -> static list, no network).
        models = (await c.get("/llm-provider/scaleway/models", headers=h)).json()
        assert "gpt-oss-120b" in models["models"]


async def test_embedder_provider_endpoints_roundtrip() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _signup(c)
        got = (await c.get("/embedder-provider", headers=h)).json()
        assert got["provider"] == "local" and got["has_key"] is False

        put = await c.put(
            "/embedder-provider",
            headers=h,
            json={"provider": "scaleway", "model": "qwen3-embedding-8b"},
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["provider"] == "scaleway"
        assert body["model"] == "qwen3-embedding-8b"
        assert body["has_key"] is False
        assert "api_key" not in put.text and "ciphertext" not in put.text

        again = (await c.get("/embedder-provider", headers=h)).json()
        assert again["provider"] == "scaleway"

        models = (await c.get("/embedder-provider/scaleway/models", headers=h)).json()
        assert "qwen3-embedding-8b" in models["models"]


async def test_provider_endpoint_requires_workspace_header() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _signup(c)
        # Missing x-workspace-id -> 422 (typed header required).
        no_ws = await c.get("/llm-provider", headers={"Authorization": h["Authorization"]})
        assert no_ws.status_code == 422
