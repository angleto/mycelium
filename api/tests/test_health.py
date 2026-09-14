"""API health smoke (no DB)."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from mycelium_api.app import create_app


async def test_healthz() -> None:
    # ASGITransport rather than TestClient: see the note in
    # test_probes_do_not_share_a_check.py -- a portal loop of its own is what
    # leaves an asyncpg transport that the suite's disposal cannot close.
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
